"""PatEx validation, ingestion, family reconstruction, and sampling."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import json
from pathlib import Path
import random
from typing import Any, Iterable, Mapping

import pandas as pd

from .schemas import (
    ApplicationRecord,
    DocumentRecord,
    IdentifierError,
    PatExInputs,
    SampleOptions,
    TransactionEvent,
    normalize_application_number,
)


class PatExValidationError(ValueError):
    """Raised when PatEx input files do not match the supported schema."""


@dataclass(frozen=True)
class ManifestBundle:
    applications: tuple[ApplicationRecord, ...]
    documents: tuple[DocumentRecord, ...]
    transactions: tuple[TransactionEvent, ...]
    counters: Mapping[str, int]


def _mapping_path() -> Path:
    return Path(__file__).parent / "resources" / "patex_2022.json"


def load_patex_mapping(path: Path | None = None) -> dict[str, Any]:
    with (path or _mapping_path()).open(encoding="utf-8") as handle:
        return json.load(handle)


def _input_files(inputs: PatExInputs) -> dict[str, Path | None]:
    return {
        "application_data": inputs.application_data,
        "transactions": inputs.transactions,
        "cms_documents": inputs.cms_documents,
        "cms_document_codes": inputs.cms_document_codes,
        "continuity_parents": inputs.continuity_parents,
        "continuity_children": inputs.continuity_children,
    }


def resolve_columns(columns: Iterable[str], aliases: Mapping[str, list[str]]) -> dict[str, str]:
    available = set(columns)
    resolved: dict[str, str] = {}
    for canonical, candidates in aliases.items():
        match = next((candidate for candidate in candidates if candidate in available), None)
        if match is not None:
            resolved[canonical] = match
    return resolved


def validate_patex_inputs(inputs: PatExInputs) -> None:
    mapping = load_patex_mapping()
    for name, path in _input_files(inputs).items():
        if path is None:
            continue
        if not path.exists():
            raise PatExValidationError(f"{path.name} does not exist: {path}")
        try:
            columns = list(pd.read_csv(path, nrows=0).columns)
        except Exception as exc:
            raise PatExValidationError(f"could not read {path.name}: {exc}") from exc
        aliases = mapping["aliases"][name]
        resolved = resolve_columns(columns, aliases)
        missing = sorted(set(aliases) - set(resolved))
        if missing:
            raise PatExValidationError(
                f"{path.name} is missing required columns: {', '.join(missing)}"
            )


def build_manifests(inputs: PatExInputs, options: SampleOptions) -> ManifestBundle:
    """Build application/document/event manifests from PatEx inputs."""

    validate_patex_inputs(inputs)
    mapping = load_patex_mapping()
    counters: dict[str, int] = {
        "application_rows": 0,
        "invalid_application_identifiers": 0,
        "non_public_applications": 0,
        "without_office_action": 0,
        "eligible_before_family_deduplication": 0,
        "selected_applications": 0,
        "unknown_document_codes": 0,
    }

    applications_frame = _read_table(
        inputs.application_data, "application_data", options.chunksize, mapping
    )
    transactions_frame = _read_table(
        inputs.transactions, "transactions", options.chunksize, mapping
    )
    documents_frame = _read_table(
        inputs.cms_documents, "cms_documents", options.chunksize, mapping
    )

    applications: dict[str, dict[str, Any]] = {}
    for row in applications_frame.to_dict(orient="records"):
        counters["application_rows"] += 1
        try:
            application_number = normalize_application_number(row["application_number"])
        except IdentifierError:
            counters["invalid_application_identifiers"] += 1
            continue
        if str(row.get("public_indicator", "")).strip().lower() not in {
            "y",
            "yes",
            "true",
            "1",
            "public",
        }:
            counters["non_public_applications"] += 1
            continue
        filing_date = _parse_date(row.get("filing_date"))
        if options.date_from and (filing_date is None or filing_date < options.date_from):
            continue
        if options.date_to and (filing_date is None or filing_date > options.date_to):
            continue
        applications[application_number] = {
            "filing_date": filing_date,
            "status": _normalize_status(row.get("status")),
        }

    document_categories = mapping["document_categories"]
    transaction_categories = mapping["transaction_categories"]
    office_action_codes = set(document_categories["office_action"])
    response_codes = set(document_categories["applicant_response"])
    transaction_office_actions = set(transaction_categories["office_action"])
    transaction_responses = set(transaction_categories["applicant_response"])

    normalized_documents: list[DocumentRecord] = []
    office_action_applications: set[str] = set()
    response_applications: set[str] = set()
    for row in documents_frame.to_dict(orient="records"):
        try:
            application_number = normalize_application_number(row["application_number"])
        except IdentifierError:
            continue
        code = str(row.get("document_code", "")).strip().upper()
        category = _document_category(code, document_categories)
        if category == "other":
            counters["unknown_document_codes"] += 1
        if code in office_action_codes:
            office_action_applications.add(application_number)
        if code in response_codes:
            response_applications.add(application_number)
        document_id = str(row.get("document_id", "")).strip()
        normalized_documents.append(
            DocumentRecord(
                application_number=application_number,
                document_id=document_id,
                document_code=code,
                document_category=category,
                recorded_date=_parse_date(row.get("document_date")),
                source_identifier=document_id,
            )
        )

    normalized_transactions: list[TransactionEvent] = []
    for row in transactions_frame.to_dict(orient="records"):
        try:
            application_number = normalize_application_number(row["application_number"])
        except IdentifierError:
            continue
        code = str(row.get("event_code", "")).strip().upper()
        if code in transaction_office_actions:
            office_action_applications.add(application_number)
        if code in transaction_responses:
            response_applications.add(application_number)
        normalized_transactions.append(
            TransactionEvent(
                application_number=application_number,
                event_code=code,
                event_date=_parse_date(row.get("event_date")),
                description=_optional_string(row.get("event_description")),
            )
        )

    eligible_numbers = sorted(set(applications) & office_action_applications)
    counters["without_office_action"] = len(applications) - len(eligible_numbers)
    counters["eligible_before_family_deduplication"] = len(eligible_numbers)
    family_ids, family_resolution = _families(inputs, eligible_numbers, options.chunksize, mapping)

    if options.one_per_family:
        representatives: dict[str, str] = {}
        for number in eligible_numbers:
            representatives.setdefault(family_ids[number], number)
        eligible_numbers = sorted(representatives.values())

    if options.sample_size < len(eligible_numbers):
        eligible_numbers = sorted(
            random.Random(options.random_seed).sample(eligible_numbers, options.sample_size)
        )
    selected = set(eligible_numbers)
    counters["selected_applications"] = len(selected)

    application_records = tuple(
        ApplicationRecord(
            application_number=number,
            filing_date=applications[number]["filing_date"],
            status=applications[number]["status"],
            family_id=family_ids[number],
            family_resolution=family_resolution[number],
            generation_eligible=number in response_applications,
            forecast_eligible=True,
        )
        for number in eligible_numbers
    )
    documents = tuple(
        sorted(
            (record for record in normalized_documents if record.application_number in selected),
            key=lambda record: (record.application_number, record.recorded_date or date.max, record.document_id),
        )
    )
    transactions = tuple(
        sorted(
            (record for record in normalized_transactions if record.application_number in selected),
            key=lambda record: (record.application_number, record.event_date or date.max, record.event_code),
        )
    )
    return ManifestBundle(application_records, documents, transactions, counters)


def _read_table(
    path: Path, name: str, chunksize: int, mapping: Mapping[str, Any]
) -> pd.DataFrame:
    pieces: list[pd.DataFrame] = []
    aliases = mapping["aliases"][name]
    for chunk in pd.read_csv(path, dtype=str, keep_default_na=False, chunksize=chunksize):
        resolved = resolve_columns(chunk.columns, aliases)
        renamed = chunk.rename(columns={source: canonical for canonical, source in resolved.items()})
        pieces.append(renamed[list(aliases)])
    if not pieces:
        return pd.DataFrame(columns=list(aliases))
    return pd.concat(pieces, ignore_index=True)


def _parse_date(value: object) -> date | None:
    text = str(value).strip() if value is not None else ""
    if not text:
        return None
    parsed = pd.to_datetime(text, errors="coerce")
    if pd.isna(parsed):
        return None
    return parsed.date()


def _optional_string(value: object) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


def _normalize_status(value: object) -> str:
    text = str(value).strip().lower()
    if "abandon" in text:
        return "abandoned"
    if "patent" in text or "allow" in text:
        return "allowed"
    if "pending" in text or "undergoing" in text:
        return "pending"
    return "unknown"


def _document_category(code: str, categories: Mapping[str, list[str]]) -> str:
    for category, values in categories.items():
        if code in values:
            return category
    return "other"


class _UnionFind:
    def __init__(self, values: Iterable[str]) -> None:
        self.parent = {value: value for value in values}

    def find(self, value: str) -> str:
        parent = self.parent[value]
        if parent != value:
            self.parent[value] = self.find(parent)
        return self.parent[value]

    def union(self, left: str, right: str) -> None:
        if left not in self.parent or right not in self.parent:
            return
        left_root, right_root = self.find(left), self.find(right)
        if left_root != right_root:
            smaller, larger = sorted((left_root, right_root))
            self.parent[larger] = smaller


def _families(
    inputs: PatExInputs,
    application_numbers: list[str],
    chunksize: int,
    mapping: Mapping[str, Any],
) -> tuple[dict[str, str], dict[str, str]]:
    continuity_available = inputs.continuity_parents is not None or inputs.continuity_children is not None
    if not continuity_available:
        return (
            {number: number for number in application_numbers},
            {number: "unavailable" for number in application_numbers},
        )

    union_find = _UnionFind(application_numbers)
    linked: set[str] = set()
    table_specs = [
        (inputs.continuity_parents, "continuity_parents", "parent_application_number"),
        (inputs.continuity_children, "continuity_children", "child_application_number"),
    ]
    for path, name, relative_column in table_specs:
        if path is None:
            continue
        frame = _read_table(path, name, chunksize, mapping)
        for row in frame.to_dict(orient="records"):
            try:
                application = normalize_application_number(row["application_number"])
                relative = normalize_application_number(row[relative_column])
            except IdentifierError:
                continue
            union_find.union(application, relative)
            if application in union_find.parent and relative in union_find.parent:
                linked.update((application, relative))

    groups: dict[str, list[str]] = {}
    for number in application_numbers:
        groups.setdefault(union_find.find(number), []).append(number)
    family_ids: dict[str, str] = {}
    resolutions: dict[str, str] = {}
    for members in groups.values():
        family_id = min(members)
        resolution = "resolved" if any(member in linked for member in members) else "singleton"
        for member in members:
            family_ids[member] = family_id
            resolutions[member] = resolution
    return family_ids, resolutions

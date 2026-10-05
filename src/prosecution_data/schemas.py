"""Shared data models and serialization helpers."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Mapping
import math
import re


class IdentifierError(ValueError):
    """Raised when a source identifier cannot be normalized safely."""


_APPLICATION_NUMBER = re.compile(r"^[0-9][0-9/,-]*$")


def normalize_application_number(value: object) -> str:
    """Normalize an application number without guessing ambiguous values."""

    if not isinstance(value, str):
        raise IdentifierError("application number must be read as a string")
    raw = value.strip()
    if not raw or not _APPLICATION_NUMBER.fullmatch(raw):
        raise IdentifierError(f"unsafe or ambiguous application number: {value!r}")
    normalized = re.sub(r"[/,-]", "", raw)
    if not normalized.isdigit() or len(normalized) < 6:
        raise IdentifierError(f"invalid application number: {value!r}")
    return normalized


@dataclass(frozen=True)
class PatExInputs:
    application_data: Path
    transactions: Path
    cms_documents: Path | None = None
    cms_document_codes: Path | None = None
    continuity_parents: Path | None = None
    continuity_children: Path | None = None
    event_codes: Path | None = None


@dataclass(frozen=True)
class SampleOptions:
    sample_size: int = 100
    random_seed: int = 42
    date_from: date | None = None
    date_to: date | None = None
    one_per_family: bool = True
    chunksize: int = 100_000


@dataclass(frozen=True)
class ApplicationRecord:
    application_number: str
    filing_date: date | None
    status: str
    family_id: str
    family_resolution: str
    generation_eligible: bool
    forecast_eligible: bool
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DocumentRecord:
    application_number: str
    document_id: str
    document_code: str
    document_category: str
    recorded_date: date | None
    source_identifier: str
    source_url: str | None = None
    media_type: str = "application/pdf"
    expected_size: int | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TransactionEvent:
    application_number: str
    event_code: str
    event_date: date | None
    description: str | None = None
    source_identifier: str = "patex"


@dataclass(frozen=True)
class ExtractedText:
    document_id: str
    source_hash: str
    status: str
    method: str | None
    text: str
    page_count: int | None
    non_whitespace_characters: int
    printable_ratio: float
    extractor_version: str
    error_category: str | None = None


@dataclass(frozen=True)
class TimelineRecord:
    application: ApplicationRecord
    events: tuple[Mapping[str, Any], ...]
    undated_events: tuple[Mapping[str, Any], ...]
    candidate_links: tuple[Mapping[str, Any], ...]
    generation_eligible: bool
    forecast_eligible: bool
    censored: bool
    reason_codes: tuple[str, ...]
    response_status: str = "unknown"
    next_event_status: str = "unknown"


def to_json_dict(value: Any) -> Any:
    """Convert supported records recursively into JSON-safe Python values."""

    if is_dataclass(value):
        return {key: to_json_dict(item) for key, item in asdict(value).items()}
    if isinstance(value, Mapping):
        return {str(key): to_json_dict(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_json_dict(item) for item in value]
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    return value

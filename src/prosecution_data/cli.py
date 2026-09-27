"""Command-line interface for the prosecution data pipeline."""

from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import date
import json
import os
from pathlib import Path
import sys
from typing import Callable, Mapping, Sequence

import httpx

from .config import AppConfig, load_config
from .extract import ExtractionSettings, SystemOcrEngine, extract_document
from .odp import AuthenticationError, OdpClient, OdpSettings, fetch_manifest
from .patex import build_manifests
from .report import build_acquisition_report
from .schemas import (
    ApplicationRecord,
    DocumentRecord,
    ExtractedText,
    PatExInputs,
    SampleOptions,
    TransactionEvent,
    to_json_dict,
)
from .storage import StateStore, read_jsonl, write_jsonl
from .timeline import TimelineSettings, build_timeline


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="prosecution-data")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--output-root", type=Path)
    subparsers = parser.add_subparsers(dest="command", required=True)

    sample = subparsers.add_parser("sample", help="build PatEx sample manifests")
    _add_patex_arguments(sample)

    fetch = subparsers.add_parser("fetch", help="download ODP documents")
    fetch.add_argument("--applications-manifest", type=Path, required=True)

    extract = subparsers.add_parser("extract", help="extract text from downloaded files")
    extract.add_argument("--documents-manifest", type=Path)

    timeline = subparsers.add_parser("build-timeline", help="build prosecution timelines")
    timeline.add_argument("--applications-manifest", type=Path)
    timeline.add_argument("--documents-manifest", type=Path)
    timeline.add_argument("--transactions-manifest", type=Path)

    run = subparsers.add_parser("run", help="run the available pipeline stages")
    _add_patex_arguments(run)
    run.add_argument("--offline", action="store_true")
    return parser


def _add_patex_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--application-data", type=Path, required=True)
    parser.add_argument("--transactions", type=Path, required=True)
    parser.add_argument("--cms-documents", type=Path, required=True)
    parser.add_argument("--cms-document-codes", type=Path, required=True)
    parser.add_argument("--continuity-parents", type=Path)
    parser.add_argument("--continuity-children", type=Path)
    parser.add_argument("--sample-size", type=int)
    parser.add_argument("--seed", type=int)
    parser.add_argument("--date-from", type=date.fromisoformat)
    parser.add_argument("--date-to", type=date.fromisoformat)
    parser.add_argument("--all-family-members", action="store_true")


def main(
    argv: Sequence[str] | None = None,
    env: Mapping[str, str] | None = None,
    *,
    http_client_factory: Callable[[], httpx.Client] = httpx.Client,
) -> int:
    args = build_parser().parse_args(argv)
    env = os.environ if env is None else env
    config = load_config(args.config, env)
    if args.output_root is not None:
        config = replace(config, output_root=args.output_root)

    if args.command == "sample":
        _run_sample(args, config)
        return 0
    if args.command == "fetch":
        return _run_fetch(args, config, http_client_factory)
    if args.command == "extract":
        return _run_extract(args, config)
    if args.command == "build-timeline":
        return _run_build_timeline(args, config)
    if args.command == "run":
        _run_sample(args, config)
        if not args.offline:
            manifest = config.output_root / "manifests" / "applications.jsonl"
            fetch_args = argparse.Namespace(applications_manifest=manifest)
            fetch_result = _run_fetch(fetch_args, config, http_client_factory)
            if fetch_result:
                return fetch_result
        _run_extract(argparse.Namespace(documents_manifest=None), config)
        _run_build_timeline(
            argparse.Namespace(
                applications_manifest=None,
                documents_manifest=None,
                transactions_manifest=None,
            ),
            config,
        )
        return 0
    raise AssertionError(f"unhandled command: {args.command}")


def _run_sample(args: argparse.Namespace, config: AppConfig) -> None:
    inputs = PatExInputs(
        application_data=args.application_data,
        transactions=args.transactions,
        cms_documents=args.cms_documents,
        cms_document_codes=args.cms_document_codes,
        continuity_parents=args.continuity_parents,
        continuity_children=args.continuity_children,
    )
    options = SampleOptions(
        sample_size=args.sample_size if args.sample_size is not None else config.sample_size,
        random_seed=args.seed if args.seed is not None else config.random_seed,
        date_from=args.date_from,
        date_to=args.date_to,
        one_per_family=not args.all_family_members,
    )
    bundle = build_manifests(inputs, options)
    manifest_dir = config.output_root / "manifests"
    write_jsonl(manifest_dir / "applications.jsonl", bundle.applications)
    write_jsonl(manifest_dir / "documents.jsonl", bundle.documents)
    write_jsonl(manifest_dir / "transactions.jsonl", bundle.transactions)
    report_dir = config.output_root / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "sampling_counters.json").write_text(
        json.dumps(dict(bundle.counters), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _run_fetch(
    args: argparse.Namespace,
    config: AppConfig,
    http_client_factory: Callable[[], httpx.Client],
) -> int:
    if not config.api_key:
        print(
            "USPTO_API_KEY is required for fetch; configure it as an environment variable.",
            file=sys.stderr,
        )
        return 2
    application_numbers = [
        str(row["application_number"]) for row in read_jsonl(args.applications_manifest)
    ]
    settings = OdpSettings(
        api_key=config.api_key,
        output_root=config.output_root,
        base_url=config.odp_base_url,
        timeout_seconds=config.timeout_seconds,
        max_retries=config.max_retries,
        fail_fast=config.fail_fast,
    )
    state_path = config.output_root / "state" / "prosecution_data.sqlite3"
    try:
        with http_client_factory() as http_client, StateStore(state_path) as state_store:
            summary = fetch_manifest(
                OdpClient(http_client, settings), application_numbers, state_store
            )
            state_rows = state_store.list_documents()
    except AuthenticationError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    write_jsonl(
        config.output_root / "manifests" / "odp_documents.jsonl",
        (row["payload"] for row in state_rows),
    )
    print(json.dumps(to_json_dict(summary), sort_keys=True))
    return 0 if summary.failed == 0 else 1


def _run_extract(args: argparse.Namespace, config: AppConfig) -> int:
    state_path = config.output_root / "state" / "prosecution_data.sqlite3"
    output_dir = config.output_root / "interim" / "text"
    output_dir.mkdir(parents=True, exist_ok=True)
    settings = ExtractionSettings(
        min_native_characters=config.min_native_characters,
        min_printable_ratio=config.min_printable_ratio,
    )
    ocr_engine = SystemOcrEngine(settings)
    extracted_records: list[ExtractedText] = []
    with StateStore(state_path) as state_store:
        for row in state_store.list_documents():
            if not row.get("path") or not row.get("sha256"):
                continue
            output_path = output_dir / f"{row['document_id']}.json"
            if row["status"] == "extracted" and output_path.exists():
                extracted = _extracted_from_dict(json.loads(output_path.read_text(encoding="utf-8")))
            else:
                extracted = extract_document(
                    Path(str(row["path"])),
                    str(row["sha256"]),
                    settings,
                    ocr_engine,
                )
                output_path.write_text(
                    json.dumps(to_json_dict(extracted), ensure_ascii=False, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
                target_status = (
                    "extracted" if extracted.status == "extracted" else "extraction_failed"
                )
                state_store.set_document_status(
                    str(row["document_id"]),
                    target_status,
                    error_category=extracted.error_category,
                )
            extracted_records.append(extracted)
    write_jsonl(
        config.output_root / "manifests" / "extracted_text.jsonl", extracted_records
    )
    return 0


def _run_build_timeline(args: argparse.Namespace, config: AppConfig) -> int:
    manifest_dir = config.output_root / "manifests"
    applications_path = args.applications_manifest or manifest_dir / "applications.jsonl"
    documents_path = args.documents_manifest or manifest_dir / "documents.jsonl"
    transactions_path = args.transactions_manifest or manifest_dir / "transactions.jsonl"
    applications = [_application_from_dict(row) for row in read_jsonl(applications_path)]
    documents = [_document_from_dict(row) for row in read_jsonl(documents_path)]
    odp_documents_path = manifest_dir / "odp_documents.jsonl"
    if odp_documents_path.exists():
        documents.extend(_document_from_dict(row) for row in read_jsonl(odp_documents_path))
    transactions = [_transaction_from_dict(row) for row in read_jsonl(transactions_path)]
    extracted_path = manifest_dir / "extracted_text.jsonl"
    extracted = (
        [_extracted_from_dict(row) for row in read_jsonl(extracted_path)]
        if extracted_path.exists()
        else []
    )
    timelines = [
        build_timeline(application, documents, transactions, extracted, TimelineSettings())
        for application in applications
    ]
    processed_dir = config.output_root / "processed"
    write_jsonl(processed_dir / "timelines.jsonl", timelines)
    processed_dir.mkdir(parents=True, exist_ok=True)
    import pandas as pd

    pd.DataFrame([_timeline_parquet_row(record) for record in timelines]).to_parquet(
        processed_dir / "timelines.parquet", index=False
    )

    counters_path = config.output_root / "reports" / "sampling_counters.json"
    counters = json.loads(counters_path.read_text(encoding="utf-8")) if counters_path.exists() else {}
    state_path = config.output_root / "state" / "prosecution_data.sqlite3"
    with StateStore(state_path) as state_store:
        document_states = state_store.list_documents()
    report = build_acquisition_report(
        sampling_counters=counters,
        applications=applications,
        documents=documents,
        document_states=document_states,
        extracted_texts=extracted,
        timelines=timelines,
    )
    report_path = config.output_root / "reports" / "acquisition_summary.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return 0


def _parse_date(value: object) -> date | None:
    return date.fromisoformat(str(value)) if value else None


def _application_from_dict(row: Mapping[str, object]) -> ApplicationRecord:
    return ApplicationRecord(
        application_number=str(row["application_number"]),
        filing_date=_parse_date(row.get("filing_date")),
        status=str(row["status"]),
        family_id=str(row["family_id"]),
        family_resolution=str(row["family_resolution"]),
        generation_eligible=bool(row["generation_eligible"]),
        forecast_eligible=bool(row["forecast_eligible"]),
        metadata=row.get("metadata", {}),  # type: ignore[arg-type]
    )


def _document_from_dict(row: Mapping[str, object]) -> DocumentRecord:
    return DocumentRecord(
        application_number=str(row["application_number"]),
        document_id=str(row["document_id"]),
        document_code=str(row["document_code"]),
        document_category=str(row["document_category"]),
        recorded_date=_parse_date(row.get("recorded_date")),
        source_identifier=str(row["source_identifier"]),
        source_url=str(row["source_url"]) if row.get("source_url") else None,
        media_type=str(row.get("media_type", "application/pdf")),
        expected_size=int(row["expected_size"]) if row.get("expected_size") is not None else None,
        metadata=row.get("metadata", {}),  # type: ignore[arg-type]
    )


def _transaction_from_dict(row: Mapping[str, object]) -> TransactionEvent:
    return TransactionEvent(
        application_number=str(row["application_number"]),
        event_code=str(row["event_code"]),
        event_date=_parse_date(row.get("event_date")),
        description=str(row["description"]) if row.get("description") else None,
        source_identifier=str(row.get("source_identifier", "patex")),
    )


def _extracted_from_dict(row: Mapping[str, object]) -> ExtractedText:
    return ExtractedText(
        document_id=str(row["document_id"]),
        source_hash=str(row["source_hash"]),
        status=str(row["status"]),
        method=str(row["method"]) if row.get("method") else None,
        text=str(row.get("text", "")),
        page_count=int(row["page_count"]) if row.get("page_count") is not None else None,
        non_whitespace_characters=int(row["non_whitespace_characters"]),
        printable_ratio=float(row["printable_ratio"]),
        extractor_version=str(row["extractor_version"]),
        error_category=str(row["error_category"]) if row.get("error_category") else None,
    )


def _timeline_parquet_row(record: object) -> dict[str, object]:
    timeline = to_json_dict(record)
    application = timeline["application"]
    return {
        "application_number": application["application_number"],
        "application_status": application["status"],
        "family_id": application["family_id"],
        "generation_eligible": timeline["generation_eligible"],
        "forecast_eligible": timeline["forecast_eligible"],
        "censored": timeline["censored"],
        "response_status": timeline["response_status"],
        "next_event_status": timeline["next_event_status"],
        "events_json": json.dumps(timeline["events"], sort_keys=True),
        "undated_events_json": json.dumps(timeline["undated_events"], sort_keys=True),
        "candidate_links_json": json.dumps(timeline["candidate_links"], sort_keys=True),
        "reason_codes_json": json.dumps(timeline["reason_codes"], sort_keys=True),
    }


def entrypoint() -> None:
    raise SystemExit(main())


if __name__ == "__main__":
    entrypoint()

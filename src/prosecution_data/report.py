"""Acquisition and cohort audit reporting."""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Iterable, Mapping, Any

from .schemas import ApplicationRecord, DocumentRecord, ExtractedText, TimelineRecord


def build_acquisition_report(
    *,
    sampling_counters: Mapping[str, int],
    applications: Iterable[ApplicationRecord],
    documents: Iterable[DocumentRecord],
    document_states: Iterable[Mapping[str, Any]],
    extracted_texts: Iterable[ExtractedText],
    timelines: Iterable[TimelineRecord],
) -> dict[str, Any]:
    applications = list(applications)
    documents = list(documents)
    document_states = list(document_states)
    extracted_texts = list(extracted_texts)
    timelines = list(timelines)

    family_counts = Counter(record.family_resolution for record in applications)
    code_counts = Counter(record.document_code for record in documents)
    download_counts = Counter(str(row.get("status", "unknown")) for row in document_states)
    extraction_statuses = Counter(record.status for record in extracted_texts)
    extraction_methods = Counter(record.method or "none" for record in extracted_texts)

    missingness: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    application_status = {record.application_number: record.status for record in applications}
    for document in documents:
        outcome = application_status.get(document.application_number, "unknown")
        missingness[document.document_category][outcome] += 1

    return {
        "sampling": dict(sampling_counters),
        "families": dict(sorted(family_counts.items())),
        "document_codes": dict(sorted(code_counts.items())),
        "unknown_document_codes": sum(
            1 for record in documents if record.document_category == "other"
        ),
        "downloads": dict(sorted(download_counts.items())),
        "extraction_statuses": dict(sorted(extraction_statuses.items())),
        "extraction_methods": dict(sorted(extraction_methods.items())),
        "missingness_by_document_type_and_outcome": {
            category: dict(sorted(outcomes.items()))
            for category, outcomes in sorted(missingness.items())
        },
        "eligibility": {
            "generation_eligible": sum(record.generation_eligible for record in timelines),
            "forecast_eligible": sum(record.forecast_eligible for record in timelines),
            "censored": sum(record.censored for record in timelines),
        },
    }

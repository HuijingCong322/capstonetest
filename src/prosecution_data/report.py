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
    attempts: Iterable[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    applications = list(applications)
    documents = list(documents)
    document_states = list(document_states)
    attempts = list(attempts)
    extracted_texts = list(extracted_texts)
    timelines = list(timelines)

    family_counts = Counter(record.family_resolution for record in applications)
    code_counts = Counter(record.document_code for record in documents)
    download_counts = Counter(str(row.get("status", "unknown")) for row in document_states)
    extraction_statuses = Counter(record.status for record in extracted_texts)
    extraction_methods = Counter(record.method or "none" for record in extracted_texts)

    attempt_outcomes = Counter(str(row.get("outcome", "unknown")) for row in attempts)
    failure_categories = Counter(
        str(row["error_category"])
        for row in attempts
        if row.get("error_category")
    )
    missingness: dict[str, dict[str, Counter[str]]] = defaultdict(
        lambda: defaultdict(Counter)
    )
    application_status = {record.application_number: record.status for record in applications}
    states_by_id = {
        str(row.get("document_id")): str(row.get("status", "unknown"))
        for row in document_states
        if row.get("document_id")
    }
    for document in documents:
        outcome = application_status.get(document.application_number, "unknown")
        acquisition_state = states_by_id.get(document.document_id, "not_acquired")
        missingness[document.document_category][outcome][acquisition_state] += 1

    return {
        "sampling": dict(sampling_counters),
        "families": dict(sorted(family_counts.items())),
        "document_codes": dict(sorted(code_counts.items())),
        "unknown_document_codes": sum(
            1 for record in documents if record.document_category == "other"
        ),
        "downloads": dict(sorted(download_counts.items())),
        "attempt_outcomes": dict(sorted(attempt_outcomes.items())),
        "failure_categories": dict(sorted(failure_categories.items())),
        "extraction_statuses": dict(sorted(extraction_statuses.items())),
        "extraction_methods": dict(sorted(extraction_methods.items())),
        "missingness_by_document_type_and_outcome": {
            category: {
                outcome: dict(sorted(states.items()))
                for outcome, states in sorted(outcomes.items())
            }
            for category, outcomes in sorted(missingness.items())
        },
        "eligibility": {
            "generation_eligible": sum(record.generation_eligible for record in timelines),
            "forecast_eligible": sum(record.forecast_eligible for record in timelines),
            "censored": sum(record.censored for record in timelines),
        },
    }

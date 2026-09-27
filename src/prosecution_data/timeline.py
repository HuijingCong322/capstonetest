"""Deterministic prosecution timeline construction."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Iterable, Mapping

from .patex import load_patex_mapping
from .schemas import (
    ApplicationRecord,
    DocumentRecord,
    ExtractedText,
    TimelineRecord,
    TransactionEvent,
)


@dataclass(frozen=True)
class TimelineSettings:
    max_interval_days: int = 365
    source_priority: Mapping[str, int] = field(
        default_factory=lambda: {"document": 0, "transaction": 1}
    )


_TRANSACTION_CATEGORIES = {
    code: category
    for category, codes in load_patex_mapping()["transaction_categories"].items()
    for code in codes
}


def build_timeline(
    application: ApplicationRecord,
    documents: Iterable[DocumentRecord],
    transactions: Iterable[TransactionEvent],
    extracted_texts: Iterable[ExtractedText],
    settings: TimelineSettings,
) -> TimelineRecord:
    application_number = application.application_number
    extracted_by_id = {record.document_id: record for record in extracted_texts}
    events: list[dict[str, Any]] = []
    for record in documents:
        if record.application_number != application_number:
            continue
        extracted = extracted_by_id.get(record.document_id)
        events.append(
            {
                "source": "document",
                "source_identifier": record.source_identifier,
                "date": record.recorded_date,
                "category": record.document_category,
                "code": record.document_code,
                "text_status": extracted.status if extracted else "not_available",
            }
        )
    for record in transactions:
        if record.application_number != application_number:
            continue
        events.append(
            {
                "source": "transaction",
                "source_identifier": record.source_identifier,
                "date": record.event_date,
                "category": _TRANSACTION_CATEGORIES.get(record.event_code, "other"),
                "code": record.event_code,
                "description": record.description,
            }
        )

    dated = [event for event in events if event["date"] is not None]
    undated = [event for event in events if event["date"] is None]
    date_counts = Counter(event["date"] for event in dated)
    for event in dated:
        event["same_date_ambiguous"] = date_counts[event["date"]] > 1
    for event in undated:
        event["same_date_ambiguous"] = False
    dated.sort(
        key=lambda event: (
            event["date"],
            settings.source_priority.get(event["source"], 999),
            event["source_identifier"],
        )
    )
    undated.sort(
        key=lambda event: (
            settings.source_priority.get(event["source"], 999),
            event["source_identifier"],
        )
    )

    candidate_links: list[dict[str, str]] = []
    for index, event in enumerate(dated):
        if event["category"] != "office_action":
            continue
        response = _next_within(
            dated,
            index + 1,
            event["date"],
            {"applicant_response"},
            settings.max_interval_days,
        )
        if response is None:
            continue
        response_index = dated.index(response)
        next_event = _next_within(
            dated,
            response_index + 1,
            response["date"],
            {"office_action", "next_examination_event", "terminal_abandonment"},
            settings.max_interval_days,
        )
        if next_event is None:
            continue
        candidate_links.append(
            {
                "type": "chronological_candidate",
                "office_action_id": event["source_identifier"],
                "response_id": response["source_identifier"],
                "next_event_id": next_event["source_identifier"],
            }
        )

    has_office_action = any(event["category"] == "office_action" for event in events)
    has_document_response = any(
        event["source"] == "document" and event["category"] == "applicant_response"
        for event in events
    )
    has_transaction_response = any(
        event["source"] == "transaction" and event["category"] == "applicant_response"
        for event in events
    )
    terminal = application.status == "abandoned" or any(
        event["category"] == "terminal_abandonment" for event in events
    )
    has_next_after_response = _has_next_event_after_response(dated)
    reason_codes: list[str] = []
    if not has_office_action:
        response_status = "not_applicable"
        next_event_status = "not_applicable"
        reason_codes.append("no_office_action")
    elif has_document_response:
        response_status = "observed"
        next_event_status = "observed" if has_next_after_response else "unknown"
        if application.status == "pending" and not has_next_after_response:
            reason_codes.append("pending_without_next_event")
    elif has_transaction_response:
        response_status = "response_document_missing"
        next_event_status = "unknown"
        reason_codes.append("response_document_missing")
    else:
        response_status = "observed_absence"
        next_event_status = "not_applicable"
        reason_codes.append("no_observed_response")
    return TimelineRecord(
        application=application,
        events=tuple(dated),
        undated_events=tuple(undated),
        candidate_links=tuple(candidate_links),
        generation_eligible=has_office_action and has_document_response,
        forecast_eligible=has_office_action,
        censored=application.status == "pending" and not terminal,
        reason_codes=tuple(reason_codes),
        response_status=response_status,
        next_event_status=next_event_status,
    )


def _next_within(
    events: list[dict[str, Any]],
    start_index: int,
    origin_date: date,
    categories: set[str],
    max_days: int,
) -> dict[str, Any] | None:
    for event in events[start_index:]:
        delta = (event["date"] - origin_date).days
        if delta > max_days:
            return None
        if delta > 0 and event["category"] in categories:
            return event
    return None


def _has_next_event_after_response(events: list[dict[str, Any]]) -> bool:
    response_date: date | None = None
    for event in events:
        if event["category"] == "applicant_response":
            response_date = event["date"]
            continue
        if response_date is not None and event["date"] > response_date and event["category"] in {
            "office_action",
            "next_examination_event",
            "terminal_abandonment",
        }:
            return True
    return False

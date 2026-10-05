from datetime import date

from prosecution_data.schemas import (
    ApplicationRecord,
    DocumentRecord,
    TransactionEvent,
)
from prosecution_data.timeline import TimelineSettings, build_timeline


def application(status: str = "pending") -> ApplicationRecord:
    return ApplicationRecord(
        application_number="12000001",
        filing_date=date(2018, 1, 1),
        status=status,
        family_id="12000001",
        family_resolution="singleton",
        generation_eligible=False,
        forecast_eligible=False,
    )


def document(identifier: str, category: str, when: date | None, app: str = "12000001") -> DocumentRecord:
    code = {"office_action": "CTNF", "applicant_response": "RESP"}.get(category, "NOA")
    return DocumentRecord(
        application_number=app,
        document_id=identifier,
        document_code=code,
        document_category=category,
        recorded_date=when,
        source_identifier=identifier,
    )


def test_order_uses_date_source_priority_and_identifier_tiebreaker() -> None:
    documents = [
        document("DOC-B", "office_action", date(2020, 1, 1)),
        document("DOC-A", "office_action", date(2020, 1, 1)),
    ]
    transactions = [
        TransactionEvent("12000001", "CTNF", date(2020, 1, 1), source_identifier="TX-A")
    ]

    timeline = build_timeline(
        application(),
        documents,
        transactions,
        extracted_texts=[],
        settings=TimelineSettings(source_priority={"document": 0, "transaction": 1}),
    )

    assert [event["source_identifier"] for event in timeline.events] == [
        "DOC-A",
        "DOC-B",
        "TX-A",
    ]
    assert all(event["same_date_ambiguous"] for event in timeline.events)


def test_candidate_link_connects_office_action_response_and_next_event() -> None:
    documents = [
        document("OA-1", "office_action", date(2020, 1, 1)),
        document("RESP-1", "applicant_response", date(2020, 2, 1)),
    ]
    transactions = [
        TransactionEvent("12000001", "NOA", date(2020, 3, 1), source_identifier="TX-NOA")
    ]

    timeline = build_timeline(
        application(), documents, transactions, [], TimelineSettings(max_interval_days=365)
    )

    assert timeline.candidate_links == (
        {
            "type": "chronological_candidate",
            "office_action_id": "OA-1",
            "response_id": "RESP-1",
            "next_event_id": "TX-NOA",
        },
    )


def test_interval_prevents_link_to_late_response() -> None:
    timeline = build_timeline(
        application(),
        [
            document("OA-1", "office_action", date(2020, 1, 1)),
            document("RESP-1", "applicant_response", date(2021, 2, 5)),
        ],
        [],
        [],
        TimelineSettings(max_interval_days=365),
    )

    assert timeline.candidate_links == ()


def test_candidate_links_never_cross_applications() -> None:
    timeline = build_timeline(
        application(),
        [
            document("OA-1", "office_action", date(2020, 1, 1)),
            document("FOREIGN-RESP", "applicant_response", date(2020, 2, 1), app="12000002"),
        ],
        [],
        [],
        TimelineSettings(),
    )

    assert timeline.candidate_links == ()


def test_same_day_events_are_ambiguous_and_not_chronologically_linked() -> None:
    same_day = date(2020, 1, 1)
    timeline = build_timeline(
        application(),
        [
            document("OA-1", "office_action", same_day),
            document("RESP-1", "applicant_response", same_day),
        ],
        [TransactionEvent("12000001", "N/=", same_day, source_identifier="TX-NOA")],
        [],
        TimelineSettings(),
    )

    assert timeline.candidate_links == ()
    assert all(event["same_date_ambiguous"] for event in timeline.events)


def test_undated_events_are_kept_separate() -> None:
    timeline = build_timeline(
        application(),
        [
            document("DATED", "office_action", date(2020, 1, 1)),
            document("UNDATED", "other", None),
        ],
        [],
        [],
        TimelineSettings(),
    )

    assert [event["source_identifier"] for event in timeline.events] == ["DATED"]
    assert [event["source_identifier"] for event in timeline.undated_events] == ["UNDATED"]


def test_missing_response_document_is_not_generation_eligible() -> None:
    timeline = build_timeline(
        application(),
        [document("OA-1", "office_action", date(2020, 1, 1))],
        [TransactionEvent("12000001", "RESP", date(2020, 2, 1), source_identifier="TX-RESP")],
        [],
        TimelineSettings(),
    )

    assert timeline.generation_eligible is False
    assert timeline.forecast_eligible is True
    assert timeline.response_status == "response_document_missing"
    assert "response_document_missing" in timeline.reason_codes


def test_pending_response_without_followup_is_censored_and_unknown() -> None:
    timeline = build_timeline(
        application("pending"),
        [
            document("OA-1", "office_action", date(2020, 1, 1)),
            document("RESP-1", "applicant_response", date(2020, 2, 1)),
        ],
        [],
        [],
        TimelineSettings(),
    )

    assert timeline.generation_eligible is True
    assert timeline.censored is True
    assert timeline.response_status == "observed"
    assert timeline.next_event_status == "unknown"
    assert "pending_without_next_event" in timeline.reason_codes


def test_observed_abandonment_is_not_censored() -> None:
    timeline = build_timeline(
        application("abandoned"),
        [document("OA-1", "office_action", date(2020, 1, 1))],
        [TransactionEvent("12000001", "ABN", date(2020, 5, 1), source_identifier="TX-ABN")],
        [],
        TimelineSettings(),
    )

    assert timeline.censored is False
    assert timeline.next_event_status == "not_applicable"


def test_no_office_action_is_not_applicable_for_benchmark_tasks() -> None:
    timeline = build_timeline(
        application(),
        [document("MISC", "other", date(2020, 1, 1))],
        [],
        [],
        TimelineSettings(),
    )

    assert timeline.generation_eligible is False
    assert timeline.forecast_eligible is False
    assert timeline.response_status == "not_applicable"
    assert timeline.next_event_status == "not_applicable"

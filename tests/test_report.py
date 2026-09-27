from datetime import date

from prosecution_data.report import build_acquisition_report
from prosecution_data.schemas import ApplicationRecord, DocumentRecord, ExtractedText, TimelineRecord


def test_report_summarizes_sampling_download_extraction_and_eligibility() -> None:
    application = ApplicationRecord(
        "12000001", date(2020, 1, 1), "pending", "FAMILY-1", "resolved", True, True
    )
    document = DocumentRecord(
        "12000001", "DOC-1", "XYZ", "other", date(2020, 2, 1), "DOC-1"
    )
    extracted = ExtractedText(
        "DOC-1", "sha", "extracted", "native_pdf", "text", 1, 4, 1.0, "pypdf-test"
    )
    timeline = TimelineRecord(
        application, (), (), (), True, True, True, ("pending_without_next_event",), "observed", "unknown"
    )

    report = build_acquisition_report(
        sampling_counters={"application_rows": 6, "selected_applications": 1},
        applications=[application],
        documents=[document],
        document_states=[{"status": "downloaded"}],
        extracted_texts=[extracted],
        timelines=[timeline],
    )

    assert report["sampling"]["application_rows"] == 6
    assert report["families"]["resolved"] == 1
    assert report["document_codes"] == {"XYZ": 1}
    assert report["unknown_document_codes"] == 1
    assert report["downloads"] == {"downloaded": 1}
    assert report["extraction_methods"] == {"native_pdf": 1}
    assert report["eligibility"] == {
        "generation_eligible": 1,
        "forecast_eligible": 1,
        "censored": 1,
    }

from pathlib import Path

from reportlab.pdfgen.canvas import Canvas

from prosecution_data.extract import (
    ExtractionSettings,
    SystemOcrEngine,
    assess_text_quality,
    extract_document,
    extract_documents,
)
from prosecution_data.schemas import to_json_dict


def make_text_pdf(path: Path, text: str) -> None:
    canvas = Canvas(str(path))
    canvas.drawString(72, 720, text)
    canvas.save()


def test_native_pdf_extraction_returns_text_quality_and_provenance(tmp_path: Path) -> None:
    path = tmp_path / "DOC-1.pdf"
    make_text_pdf(path, "A patent claim with enough readable native text.")

    extracted = extract_document(
        path,
        "source-sha",
        ExtractionSettings(min_native_characters=10, min_printable_ratio=0.8),
        ocr_engine=None,
    )

    assert "patent claim" in extracted.text
    assert extracted.document_id == "DOC-1"
    assert extracted.source_hash == "source-sha"
    assert extracted.page_count == 1
    assert extracted.method == "native_pdf"
    assert extracted.status == "extracted"
    assert extracted.non_whitespace_characters >= 10
    assert extracted.printable_ratio == 1.0
    assert extracted.extractor_version.startswith("pypdf-")


def test_quality_counts_non_whitespace_and_printable_ratio_exactly() -> None:
    quality = assess_text_quality("A B\n\x00")

    assert quality.non_whitespace_characters == 3
    assert quality.printable_ratio == 0.8


def test_quality_thresholds_are_inclusive() -> None:
    settings = ExtractionSettings(min_native_characters=3, min_printable_ratio=0.8)

    assert assess_text_quality("A B\n\x00").is_sufficient(settings) is True
    assert assess_text_quality("A \n\x00").is_sufficient(settings) is False


class FakeOcr:
    def __init__(self, *, available: bool, text: str = "") -> None:
        self._available = available
        self.text = text
        self.paths: list[Path] = []

    def available(self) -> bool:
        return self._available

    def extract(self, path: Path) -> str:
        self.paths.append(path)
        return self.text


def test_ocr_fallback_extracts_low_quality_native_pdf(tmp_path: Path) -> None:
    path = tmp_path / "DOC-2.pdf"
    make_text_pdf(path, "x")
    ocr = FakeOcr(available=True, text="Readable OCR text for a patent response.")

    extracted = extract_document(
        path,
        "sha",
        ExtractionSettings(min_native_characters=10),
        ocr,
    )

    assert extracted.status == "extracted"
    assert extracted.method == "ocr"
    assert extracted.text == "Readable OCR text for a patent response."
    assert extracted.error_category is None
    assert ocr.paths == [path]


def test_ocr_unavailable_is_explicit_and_json_safe(tmp_path: Path) -> None:
    path = tmp_path / "DOC-3.pdf"
    make_text_pdf(path, "x")

    extracted = extract_document(
        path,
        "sha",
        ExtractionSettings(min_native_characters=10),
        FakeOcr(available=False),
    )

    assert extracted.status == "extraction_failed"
    assert extracted.error_category == "ocr_unavailable"
    assert to_json_dict(extracted)["error_category"] == "ocr_unavailable"


def test_corrupt_pdf_returns_error_status(tmp_path: Path) -> None:
    path = tmp_path / "BROKEN.pdf"
    path.write_bytes(b"not a pdf")

    extracted = extract_document(path, "sha", ExtractionSettings(), FakeOcr(available=True))

    assert extracted.status == "extraction_failed"
    assert extracted.error_category == "corrupt"


def test_unsupported_media_returns_error_status(tmp_path: Path) -> None:
    path = tmp_path / "DOC-4.txt"
    path.write_text("plain text", encoding="utf-8")

    extracted = extract_document(path, "sha", ExtractionSettings(), None)

    assert extracted.error_category == "unsupported_media"


def test_extract_documents_continues_after_a_corrupt_file(tmp_path: Path) -> None:
    broken = tmp_path / "BROKEN.pdf"
    broken.write_bytes(b"bad")
    good = tmp_path / "GOOD.pdf"
    make_text_pdf(good, "Enough native content to pass the configured threshold.")

    records = extract_documents(
        [(broken, "bad-sha"), (good, "good-sha")],
        ExtractionSettings(min_native_characters=10),
        None,
    )

    assert [record.status for record in records] == ["extraction_failed", "extracted"]


def test_system_ocr_availability_requires_both_executables() -> None:
    found = {"pdftoppm": "/bin/pdftoppm", "tesseract": None}
    engine = SystemOcrEngine(which=lambda name: found[name])

    assert engine.available() is False

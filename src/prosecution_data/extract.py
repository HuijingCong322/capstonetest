"""Native PDF extraction with optional OCR fallback."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Protocol
import shutil
import subprocess
import tempfile

import pypdf
from pypdf import PdfReader

from .schemas import ExtractedText


@dataclass(frozen=True)
class ExtractionSettings:
    min_native_characters: int = 80
    min_printable_ratio: float = 0.85
    pdftoppm_command: str = "pdftoppm"
    tesseract_command: str = "tesseract"
    render_dpi: int = 200


@dataclass(frozen=True)
class TextQuality:
    non_whitespace_characters: int
    printable_ratio: float

    def is_sufficient(self, settings: ExtractionSettings) -> bool:
        return (
            self.non_whitespace_characters >= settings.min_native_characters
            and self.printable_ratio >= settings.min_printable_ratio
        )


class OcrEngine(Protocol):
    def available(self) -> bool: ...

    def extract(self, path: Path) -> str: ...


class SystemOcrEngine:
    def __init__(
        self,
        settings: ExtractionSettings | None = None,
        *,
        which: Callable[[str], str | None] = shutil.which,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> None:
        self.settings = settings or ExtractionSettings()
        self.which = which
        self.runner = runner

    def available(self) -> bool:
        return bool(
            self.which(self.settings.pdftoppm_command)
            and self.which(self.settings.tesseract_command)
        )

    def extract(self, path: Path) -> str:
        if not self.available():
            raise FileNotFoundError("pdftoppm and tesseract are required for OCR")
        with tempfile.TemporaryDirectory(prefix="prosecution-ocr-") as temp_dir:
            prefix = Path(temp_dir) / "page"
            self.runner(
                [
                    self.settings.pdftoppm_command,
                    "-r",
                    str(self.settings.render_dpi),
                    "-png",
                    str(path),
                    str(prefix),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            page_text: list[str] = []
            for image_path in sorted(Path(temp_dir).glob("page-*.png")):
                result = self.runner(
                    [self.settings.tesseract_command, str(image_path), "stdout"],
                    check=True,
                    capture_output=True,
                    text=True,
                )
                page_text.append(result.stdout)
            return "\n\n".join(page_text)


def assess_text_quality(text: str) -> TextQuality:
    if not text:
        return TextQuality(0, 0.0)
    non_whitespace = sum(1 for character in text if not character.isspace())
    printable = sum(
        1
        for character in text
        if character.isprintable() or character in {"\n", "\r", "\t"}
    )
    return TextQuality(non_whitespace, printable / len(text))


def extract_document(
    path: Path,
    source_hash: str,
    settings: ExtractionSettings,
    ocr_engine: OcrEngine | None,
) -> ExtractedText:
    path = Path(path)
    if path.suffix.lower() != ".pdf":
        return _error_record(path, source_hash, "unsupported_media")
    try:
        reader = PdfReader(path)
        page_text = [(page.extract_text() or "") for page in reader.pages]
        native_text = "\n\n".join(page_text)
        quality = assess_text_quality(native_text)
        if quality.is_sufficient(settings):
            return ExtractedText(
                document_id=path.stem,
                source_hash=source_hash,
                status="extracted",
                method="native_pdf",
                text=native_text,
                page_count=len(reader.pages),
                non_whitespace_characters=quality.non_whitespace_characters,
                printable_ratio=quality.printable_ratio,
                extractor_version=f"pypdf-{pypdf.__version__}",
            )
        if ocr_engine is None or not ocr_engine.available():
            return _error_record(
                path,
                source_hash,
                "ocr_unavailable",
                text=native_text,
                page_count=len(reader.pages),
                quality=quality,
            )
        try:
            ocr_text = ocr_engine.extract(path)
        except Exception:
            return _error_record(
                path,
                source_hash,
                "ocr_failed",
                text=native_text,
                page_count=len(reader.pages),
                quality=quality,
            )
        ocr_quality = assess_text_quality(ocr_text)
        if not ocr_text.strip():
            return _error_record(
                path,
                source_hash,
                "ocr_empty",
                page_count=len(reader.pages),
                quality=ocr_quality,
            )
        return ExtractedText(
            document_id=path.stem,
            source_hash=source_hash,
            status="extracted",
            method="ocr",
            text=ocr_text,
            page_count=len(reader.pages),
            non_whitespace_characters=ocr_quality.non_whitespace_characters,
            printable_ratio=ocr_quality.printable_ratio,
            extractor_version=f"pypdf-{pypdf.__version__}+system-ocr",
        )
    except Exception:
        return _error_record(path, source_hash, "corrupt")


def _error_record(
    path: Path,
    source_hash: str,
    category: str,
    *,
    text: str = "",
    page_count: int | None = None,
    quality: TextQuality | None = None,
) -> ExtractedText:
    quality = quality or assess_text_quality(text)
    return ExtractedText(
        document_id=path.stem,
        source_hash=source_hash,
        status="extraction_failed",
        method=None,
        text=text,
        page_count=page_count,
        non_whitespace_characters=quality.non_whitespace_characters,
        printable_ratio=quality.printable_ratio,
        extractor_version=f"pypdf-{pypdf.__version__}",
        error_category=category,
    )


def extract_documents(
    documents: Iterable[tuple[Path, str]],
    settings: ExtractionSettings,
    ocr_engine: OcrEngine | None,
) -> list[ExtractedText]:
    return [
        extract_document(path, source_hash, settings, ocr_engine)
        for path, source_hash in documents
    ]

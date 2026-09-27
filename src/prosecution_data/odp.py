"""USPTO Open Data Portal Patent File Wrapper client."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import quote
import random as random_module
import time

import httpx

from .schemas import DocumentRecord, normalize_application_number
from .storage import StateStore, StoredFile, safe_document_path, store_download


class AuthenticationError(RuntimeError):
    pass


class RateLimitError(RuntimeError):
    pass


class OdpSchemaError(RuntimeError):
    pass


class RetryExhausted(RuntimeError):
    pass


@dataclass(frozen=True)
class OdpSettings:
    api_key: str | None
    output_root: Path
    base_url: str = "https://api.uspto.gov"
    list_template: str = "/api/v1/patent/applications/{application_number}/documents"
    download_template: str = "/api/v1/download/applications/{application_number}/{document_id}.pdf"
    rows_per_page: int = 50
    timeout_seconds: float = 30.0
    max_retries: int = 4
    backoff_base_seconds: float = 0.5
    max_backoff_seconds: float = 30.0
    fail_fast: bool = False


@dataclass(frozen=True)
class FetchSummary:
    applications: int = 0
    discovered: int = 0
    downloaded: int = 0
    reused: int = 0
    failed: int = 0


_DOCUMENT_CODE_CATEGORIES = {
    "CTNF": "office_action",
    "CTFR": "office_action",
    "RESP": "applicant_response",
    "A.NE": "applicant_response",
    "A.AF": "applicant_response",
    "AMND": "applicant_response",
    "NOA": "next_examination_event",
    "ABN": "next_examination_event",
}


class OdpClient:
    def __init__(
        self,
        http_client: httpx.Client,
        settings: OdpSettings,
        *,
        sleep: Callable[[float], None] = time.sleep,
        jitter: Callable[[], float] = random_module.random,
    ) -> None:
        if not settings.api_key:
            raise AuthenticationError("USPTO_API_KEY is required for live ODP access")
        self.http_client = http_client
        self.settings = settings
        self.sleep = sleep
        self.jitter = jitter

    @property
    def _headers(self) -> dict[str, str]:
        return {"X-API-KEY": self.settings.api_key or ""}

    def list_documents(self, application_number: str) -> list[DocumentRecord]:
        application_number = normalize_application_number(application_number)
        path = self.settings.list_template.format(
            application_number=quote(application_number, safe="")
        )
        url = f"{self.settings.base_url.rstrip('/')}{path}"
        start = 0
        records: list[DocumentRecord] = []
        while True:
            response = self._request(
                "GET",
                url,
                params={"start": start, "rows": self.settings.rows_per_page},
            )
            payload = response.json()
            items = self._document_items(payload)
            records.extend(self._parse_document(item, application_number) for item in items)
            total = _integer(payload.get("totalRecordCount") or payload.get("total"))
            if not items or (total is not None and len(records) >= total):
                break
            if total is None and len(items) < self.settings.rows_per_page:
                break
            start += len(items)
        return records

    def _request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        stream: bool = False,
    ) -> httpx.Response:
        last_error: Exception | None = None
        for attempt in range(self.settings.max_retries + 1):
            try:
                request = self.http_client.build_request(
                    method,
                    url,
                    headers=self._headers,
                    params=params,
                    timeout=self.settings.timeout_seconds,
                )
                response = self.http_client.send(request, stream=stream)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = exc
                if attempt >= self.settings.max_retries:
                    raise RetryExhausted("ODP transport retries exhausted") from exc
                self.sleep(self._backoff(attempt, None))
                continue
            if response.status_code in {401, 403}:
                response.close()
                raise AuthenticationError("ODP authentication failed; verify USPTO_API_KEY")
            if response.status_code == 429 or response.status_code >= 500:
                retry_after = response.headers.get("Retry-After")
                response.close()
                if attempt >= self.settings.max_retries:
                    raise RetryExhausted(
                        f"ODP retries exhausted after HTTP {response.status_code}"
                    )
                self.sleep(self._backoff(attempt, retry_after))
                continue
            response.raise_for_status()
            return response
        raise RetryExhausted("ODP retries exhausted") from last_error

    def _backoff(self, attempt: int, retry_after: str | None) -> float:
        if retry_after:
            try:
                return min(float(retry_after), self.settings.max_backoff_seconds)
            except ValueError:
                pass
        delay = self.settings.backoff_base_seconds * (2**attempt)
        delay += self.jitter() * self.settings.backoff_base_seconds
        return min(delay, self.settings.max_backoff_seconds)

    @staticmethod
    def _document_items(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
        for key in ("documentBag", "patentFileWrapperDocumentDataBag"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
        fields = ", ".join(sorted(str(key) for key in payload))
        raise OdpSchemaError(f"unsupported ODP document response fields: {fields}")

    @staticmethod
    def _parse_document(item: Mapping[str, Any], fallback_application: str) -> DocumentRecord:
        application_number = normalize_application_number(
            str(item.get("applicationNumberText") or item.get("applicationNumber") or fallback_application)
        )
        document_id = str(item.get("documentIdentifier") or item.get("documentId") or "").strip()
        code = str(item.get("documentCode") or "").strip().upper()
        raw_date = item.get("officialDate") or item.get("documentDate")
        recorded_date: date | None = None
        if raw_date:
            try:
                recorded_date = date.fromisoformat(str(raw_date)[:10])
            except ValueError:
                recorded_date = None
        size_value = item.get("documentSizeQuantity") or item.get("documentSize")
        try:
            expected_size = int(size_value) if size_value not in (None, "") else None
        except (TypeError, ValueError):
            expected_size = None
        return DocumentRecord(
            application_number=application_number,
            document_id=document_id,
            document_code=code,
            document_category=_DOCUMENT_CODE_CATEGORIES.get(code, "other"),
            recorded_date=recorded_date,
            source_identifier=document_id,
            media_type=str(item.get("mimeTypeIdentifier") or item.get("mimeType") or "application/pdf"),
            expected_size=expected_size,
            metadata={"description": item.get("documentDescription")},
        )

    def download_document(self, record: DocumentRecord) -> StoredFile:
        destination = safe_document_path(
            self.settings.output_root / "raw" / "odp",
            record.application_number,
            record.document_id,
            ".pdf",
        )
        if record.source_url:
            url = record.source_url
        else:
            path = self.settings.download_template.format(
                application_number=quote(record.application_number, safe=""),
                document_id=quote(record.document_id, safe=""),
            )
            url = f"{self.settings.base_url.rstrip('/')}{path}"
        response = self._request("GET", url, stream=True)
        try:
            return store_download(
                response.iter_bytes(), destination, expected_size=record.expected_size
            )
        finally:
            response.close()


def fetch_manifest(
    client: OdpClient,
    application_numbers: Iterable[str],
    state_store: StateStore,
) -> FetchSummary:
    applications = discovered = downloaded = reused = failed = 0
    for application_number in application_numbers:
        applications += 1
        try:
            documents = client.list_documents(application_number)
        except Exception:
            if client.settings.fail_fast:
                raise
            continue
        discovered += len(documents)
        for record in documents:
            state_store.upsert_document(record)
            try:
                stored = client.download_document(record)
                state_store.record_attempt(record.document_id, outcome="downloaded")
                state_store.set_document_status(
                    record.document_id,
                    "downloaded",
                    sha256=stored.sha256,
                    path=stored.path,
                )
                if stored.reused:
                    reused += 1
                else:
                    downloaded += 1
            except Exception as exc:
                failed += 1
                state_store.record_attempt(
                    record.document_id,
                    outcome="failed",
                    error=str(exc),
                    secret_values=(client.settings.api_key or "",),
                )
                state_store.set_document_status(
                    record.document_id,
                    "failed",
                    error_category=type(exc).__name__,
                )
                if client.settings.fail_fast:
                    raise
    return FetchSummary(applications, discovered, downloaded, reused, failed)


def _integer(value: object) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None

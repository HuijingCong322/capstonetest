import json
from pathlib import Path

import httpx
import pytest

from prosecution_data.odp import (
    AuthenticationError,
    RetryExhausted,
    OdpClient,
    OdpSchemaError,
    OdpSettings,
    fetch_manifest,
)
from prosecution_data.schemas import DocumentRecord
from prosecution_data.storage import StateStore


FIXTURES = Path(__file__).parent / "fixtures" / "odp"


def fixture_payload(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_auth_missing_key_fails_before_any_request(tmp_path: Path) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={})

    client = httpx.Client(transport=httpx.MockTransport(handler))

    with pytest.raises(AuthenticationError, match="USPTO_API_KEY"):
        OdpClient(client, OdpSettings(api_key=None, output_root=tmp_path))

    assert calls == 0


def test_auth_uses_api_key_header_without_leaking_secret(tmp_path: Path) -> None:
    secret = "private-key"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-API-KEY"] == secret
        assert "Authorization" not in request.headers
        return httpx.Response(401, request=request, text=f"bad key {secret}")

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport) as http_client:
        client = OdpClient(http_client, OdpSettings(api_key=secret, output_root=tmp_path))
        with pytest.raises(AuthenticationError) as exc_info:
            client.list_documents("12000001")

    assert secret not in str(exc_info.value)


@pytest.mark.parametrize(
    ("fixture_name", "document_id", "category"),
    [
        ("documents_page_1.json", "ODP-DOC-1", "office_action"),
        ("documents_page_2.json", "ODP-DOC-2", "applicant_response"),
    ],
)
def test_schema_accepts_supported_document_envelopes(
    tmp_path: Path, fixture_name: str, document_id: str, category: str
) -> None:
    payload = fixture_payload(fixture_name)
    transport = httpx.MockTransport(lambda request: httpx.Response(200, request=request, json=payload))

    with httpx.Client(transport=transport) as http_client:
        client = OdpClient(http_client, OdpSettings(api_key="key", output_root=tmp_path))
        documents = client.list_documents("12000001")

    assert len(documents) == 1
    assert documents[0].document_id == document_id
    assert documents[0].document_category == category


def test_schema_error_lists_fields_without_response_values(tmp_path: Path) -> None:
    secret_value = "do-not-print-this"
    payload = {"unexpected": [], "secretToken": secret_value}
    transport = httpx.MockTransport(lambda request: httpx.Response(200, request=request, json=payload))

    with httpx.Client(transport=transport) as http_client:
        client = OdpClient(http_client, OdpSettings(api_key="key", output_root=tmp_path))
        with pytest.raises(OdpSchemaError) as exc_info:
            client.list_documents("12000001")

    message = str(exc_info.value)
    assert "unexpected" in message
    assert "secretToken" in message
    assert secret_value not in message


def test_pagination_collects_all_documents(tmp_path: Path) -> None:
    starts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        start = int(request.url.params["start"])
        starts.append(start)
        items = [
            {
                "applicationNumberText": "12000001",
                "documentIdentifier": f"DOC-{index}",
                "documentCode": "CTNF",
                "officialDate": "2020-01-01",
            }
            for index in ((1, 2) if start == 0 else (3,))
        ]
        return httpx.Response(
            200,
            request=request,
            json={"documentBag": items, "count": len(items), "totalRecordCount": 3},
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        client = OdpClient(
            http_client,
            OdpSettings(api_key="key", output_root=tmp_path, rows_per_page=2),
        )
        documents = client.list_documents("12000001")

    assert starts == [0, 2]
    assert [record.document_id for record in documents] == ["DOC-1", "DOC-2", "DOC-3"]


def test_retry_honors_retry_after_then_succeeds(tmp_path: Path) -> None:
    attempts = 0
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(429, request=request, headers={"Retry-After": "3"})
        return httpx.Response(200, request=request, json=fixture_payload("documents_page_1.json"))

    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        client = OdpClient(
            http_client,
            OdpSettings(api_key="key", output_root=tmp_path),
            sleep=sleeps.append,
            jitter=lambda: 0.0,
        )
        documents = client.list_documents("12000001")

    assert len(documents) == 1
    assert attempts == 2
    assert sleeps == [3.0]


def test_retry_exhaustion_is_bounded(tmp_path: Path) -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(503, request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        client = OdpClient(
            http_client,
            OdpSettings(api_key="key", output_root=tmp_path, max_retries=2),
            sleep=lambda seconds: None,
            jitter=lambda: 0.0,
        )
        with pytest.raises(RetryExhausted):
            client.list_documents("12000001")

    assert attempts == 3


def test_retry_recovers_from_transport_timeout(tmp_path: Path) -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.ReadTimeout("timed out", request=request)
        return httpx.Response(200, request=request, json=fixture_payload("documents_page_1.json"))

    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        client = OdpClient(
            http_client,
            OdpSettings(api_key="key", output_root=tmp_path),
            sleep=lambda seconds: None,
            jitter=lambda: 0.0,
        )
        assert len(client.list_documents("12000001")) == 1

    assert attempts == 2


def test_non_retryable_client_error_is_not_retried(tmp_path: Path) -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(404, request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        client = OdpClient(http_client, OdpSettings(api_key="key", output_root=tmp_path))
        with pytest.raises(httpx.HTTPStatusError):
            client.list_documents("12000001")

    assert attempts == 1


def test_download_streams_to_safe_path_and_records_hash(tmp_path: Path) -> None:
    payload = b"pdf-payload"
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, request=request, content=payload)
    )
    record = DocumentRecord(
        application_number="12000001",
        document_id="DOC-1",
        document_code="CTNF",
        document_category="office_action",
        recorded_date=None,
        source_identifier="DOC-1",
        expected_size=len(payload),
    )

    with httpx.Client(transport=transport) as http_client:
        client = OdpClient(http_client, OdpSettings(api_key="key", output_root=tmp_path))
        stored = client.download_document(record)

    assert stored.path == tmp_path / "raw" / "odp" / "12000001" / "DOC-1.pdf"
    assert stored.path.read_bytes() == payload


def test_download_size_mismatch_cleans_temporary_file(tmp_path: Path) -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, request=request, content=b"short")
    )
    record = DocumentRecord(
        application_number="12000001",
        document_id="DOC-1",
        document_code="CTNF",
        document_category="office_action",
        recorded_date=None,
        source_identifier="DOC-1",
        expected_size=100,
    )

    with httpx.Client(transport=transport) as http_client:
        client = OdpClient(http_client, OdpSettings(api_key="key", output_root=tmp_path))
        with pytest.raises(ValueError, match="expected 100 bytes"):
            client.download_document(record)

    raw = tmp_path / "raw" / "odp" / "12000001"
    assert not (raw / "DOC-1.pdf").exists()
    assert not list(raw.glob("*.tmp"))


def test_interrupted_download_cleans_temporary_file(tmp_path: Path) -> None:
    class BrokenStream(httpx.SyncByteStream):
        def __iter__(self):
            yield b"partial"
            raise httpx.ReadError("connection lost")

    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, request=request, stream=BrokenStream())
    )
    record = DocumentRecord(
        application_number="12000001",
        document_id="DOC-1",
        document_code="CTNF",
        document_category="office_action",
        recorded_date=None,
        source_identifier="DOC-1",
    )

    with httpx.Client(transport=transport) as http_client:
        client = OdpClient(http_client, OdpSettings(api_key="key", output_root=tmp_path))
        with pytest.raises(httpx.ReadError):
            client.download_document(record)

    raw = tmp_path / "raw" / "odp" / "12000001"
    assert not (raw / "DOC-1.pdf").exists()
    assert not list(raw.glob("*.tmp"))


def test_fetch_manifest_continues_after_document_download_failure(tmp_path: Path) -> None:
    payload = fixture_payload("documents_page_1.json")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/documents"):
            return httpx.Response(200, request=request, json=payload)
        return httpx.Response(404, request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        client = OdpClient(http_client, OdpSettings(api_key="key", output_root=tmp_path))
        with StateStore(tmp_path / "state.sqlite3") as state:
            summary = fetch_manifest(client, ["12000001"], state)
            assert state.get_document_status("ODP-DOC-1") == "failed"

    assert summary.applications == 1
    assert summary.discovered == 1
    assert summary.failed == 1

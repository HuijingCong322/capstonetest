import hashlib
from pathlib import Path

import pytest

from prosecution_data.schemas import DocumentRecord
from prosecution_data.storage import (
    InvalidStateTransition,
    StateStore,
    UnsafePathError,
    SizeMismatchError,
    read_jsonl,
    safe_document_path,
    store_download,
    write_jsonl,
)


def sample_document() -> DocumentRecord:
    return DocumentRecord(
        application_number="12000001",
        document_id="DOC-1",
        document_code="CTNF",
        document_category="office_action",
        recorded_date=None,
        source_identifier="DOC-1",
    )


def test_safe_path_builds_expected_document_path(tmp_path: Path) -> None:
    assert safe_document_path(tmp_path, "12000001", "DOC-1", ".pdf") == (
        tmp_path / "12000001" / "DOC-1.pdf"
    )


@pytest.mark.parametrize(
    ("application_number", "document_id", "suffix"),
    [
        ("", "DOC-1", ".pdf"),
        ("../12000001", "DOC-1", ".pdf"),
        ("12000001", "../secret", ".pdf"),
        ("12000001", "", ".pdf"),
        ("12000001", "DOC-1", "/tmp.pdf"),
    ],
)
def test_safe_path_rejects_empty_or_traversal_values(
    tmp_path: Path, application_number: str, document_id: str, suffix: str
) -> None:
    with pytest.raises(UnsafePathError):
        safe_document_path(tmp_path, application_number, document_id, suffix)


def test_state_store_creates_schema_and_records_valid_state_transitions(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite3"

    with StateStore(database) as store:
        store.upsert_document(sample_document())
        assert store.get_document_status("DOC-1") == "pending"
        store.set_document_status("DOC-1", "downloaded")
        store.set_document_status("DOC-1", "extracted")
        assert store.get_document_status("DOC-1") == "extracted"

    with StateStore(database) as reopened:
        assert reopened.get_document_status("DOC-1") == "extracted"


def test_state_store_rejects_invalid_transition(tmp_path: Path) -> None:
    with StateStore(tmp_path / "state.sqlite3") as store:
        store.upsert_document(sample_document())
        store.set_document_status("DOC-1", "downloaded")

        with pytest.raises(InvalidStateTransition):
            store.set_document_status("DOC-1", "pending")


def test_attempt_storage_redacts_secrets_and_authorization_headers(tmp_path: Path) -> None:
    secret = "private-api-key"
    with StateStore(tmp_path / "state.sqlite3") as store:
        store.upsert_document(sample_document())
        store.record_attempt(
            "DOC-1",
            outcome="failed",
            error=f"request failed with {secret}",
            headers={"Authorization": f"Bearer {secret}", "X-API-KEY": secret, "Retry-After": "2"},
            secret_values=(secret,),
        )
        attempt = store.list_attempts("DOC-1")[0]

    rendered = repr(attempt)
    assert secret not in rendered
    assert "Authorization" not in rendered
    assert "X-API-KEY" not in rendered
    assert attempt["error"] == "request failed with [REDACTED]"
    assert attempt["headers"] == {"Retry-After": "2"}


def test_atomic_download_writes_hash_and_reuses_matching_file(tmp_path: Path) -> None:
    destination = tmp_path / "raw" / "DOC-1.pdf"
    payload = b"patent document"
    expected_hash = hashlib.sha256(payload).hexdigest()

    first = store_download(iter((payload,)), destination, expected_size=len(payload))
    second = store_download(iter((payload,)), destination, expected_size=len(payload))

    assert destination.read_bytes() == payload
    assert first.sha256 == expected_hash
    assert first.reused is False
    assert second.sha256 == expected_hash
    assert second.reused is True
    assert not list(destination.parent.glob("*.tmp"))


def test_hash_mismatch_quarantines_old_file_before_accepting_new_one(tmp_path: Path) -> None:
    destination = tmp_path / "DOC-1.pdf"
    old_payload = b"old"
    new_payload = b"new"
    destination.write_bytes(old_payload)
    old_hash = hashlib.sha256(old_payload).hexdigest()

    stored = store_download(iter((new_payload,)), destination)

    quarantine = destination.with_name(f"DOC-1.pdf.quarantine.{old_hash}")
    assert quarantine.read_bytes() == old_payload
    assert destination.read_bytes() == new_payload
    assert stored.reused is False


def test_interrupted_atomic_download_leaves_no_destination_or_temp_file(tmp_path: Path) -> None:
    destination = tmp_path / "DOC-1.pdf"

    def interrupted():
        yield b"partial"
        raise OSError("connection lost")

    with pytest.raises(OSError, match="connection lost"):
        store_download(interrupted(), destination)

    assert not destination.exists()
    assert not list(tmp_path.glob("*.tmp"))


def test_size_mismatch_leaves_no_valid_destination(tmp_path: Path) -> None:
    destination = tmp_path / "DOC-1.pdf"

    with pytest.raises(SizeMismatchError):
        store_download(iter((b"short",)), destination, expected_size=50)

    assert not destination.exists()


def test_jsonl_round_trip_is_stable_and_newline_delimited(tmp_path: Path) -> None:
    path = tmp_path / "manifest.jsonl"
    rows = [{"b": 2, "a": 1}, {"nested": {"value": True}}]

    write_jsonl(path, rows)

    assert path.read_text(encoding="utf-8") == (
        '{"a": 1, "b": 2}\n{"nested": {"value": true}}\n'
    )
    assert list(read_jsonl(path)) == rows

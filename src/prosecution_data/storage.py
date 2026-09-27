"""Durable state, safe paths, hashing, and manifest persistence."""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO
import hashlib
import json
import os
import re
import sqlite3
import tempfile

from .schemas import DocumentRecord, to_json_dict


class UnsafePathError(ValueError):
    pass


class InvalidStateTransition(ValueError):
    pass


class SizeMismatchError(ValueError):
    pass


_SAFE_APPLICATION = re.compile(r"^[0-9]+$")
_SAFE_DOCUMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_SAFE_SUFFIX = re.compile(r"^\.[A-Za-z0-9]+$")


def safe_document_path(
    root: Path, application_number: str, document_id: str, suffix: str
) -> Path:
    if not _SAFE_APPLICATION.fullmatch(application_number):
        raise UnsafePathError(f"unsafe application number: {application_number!r}")
    if not _SAFE_DOCUMENT.fullmatch(document_id) or document_id in {".", ".."}:
        raise UnsafePathError(f"unsafe document identifier: {document_id!r}")
    if not _SAFE_SUFFIX.fullmatch(suffix):
        raise UnsafePathError(f"unsafe suffix: {suffix!r}")
    return Path(root) / application_number / f"{document_id}{suffix}"


_TRANSITIONS = {
    "pending": {"downloaded", "failed"},
    "failed": {"pending", "downloaded", "failed"},
    "downloaded": {"extracted", "extraction_failed"},
    "extraction_failed": {"extracted", "extraction_failed"},
    "extracted": {"extracted"},
}


class StateStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self._migrate()

    def __enter__(self) -> "StateStore":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def close(self) -> None:
        self.connection.close()

    def _migrate(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS applications (
                application_number TEXT PRIMARY KEY,
                payload_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS documents (
                document_id TEXT PRIMARY KEY,
                application_number TEXT NOT NULL,
                status TEXT NOT NULL,
                sha256 TEXT,
                path TEXT,
                error_category TEXT,
                payload_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                document_id TEXT NOT NULL,
                outcome TEXT NOT NULL,
                error TEXT,
                headers_json TEXT NOT NULL
            );
            """
        )
        self.connection.commit()

    def upsert_document(self, record: DocumentRecord, status: str = "pending") -> None:
        self.connection.execute(
            """
            INSERT INTO documents(document_id, application_number, status, payload_json)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(document_id) DO UPDATE SET
                application_number=excluded.application_number,
                payload_json=excluded.payload_json
            """,
            (
                record.document_id,
                record.application_number,
                status,
                json.dumps(to_json_dict(record), sort_keys=True),
            ),
        )
        self.connection.commit()

    def get_document_status(self, document_id: str) -> str | None:
        row = self.connection.execute(
            "SELECT status FROM documents WHERE document_id = ?", (document_id,)
        ).fetchone()
        return str(row["status"]) if row else None

    def set_document_status(
        self,
        document_id: str,
        status: str,
        *,
        sha256: str | None = None,
        path: Path | None = None,
        error_category: str | None = None,
    ) -> None:
        current = self.get_document_status(document_id)
        if current is None:
            raise KeyError(document_id)
        if status not in _TRANSITIONS.get(current, set()):
            raise InvalidStateTransition(f"cannot transition {current!r} to {status!r}")
        self.connection.execute(
            """
            UPDATE documents
            SET status = ?, sha256 = COALESCE(?, sha256), path = COALESCE(?, path),
                error_category = ?
            WHERE document_id = ?
            """,
            (status, sha256, str(path) if path else None, error_category, document_id),
        )
        self.connection.commit()

    def record_attempt(
        self,
        document_id: str,
        *,
        outcome: str,
        error: str | None = None,
        headers: Mapping[str, str] | None = None,
        secret_values: Iterable[str] = (),
    ) -> None:
        safe_headers = {
            key: value
            for key, value in (headers or {}).items()
            if key.lower() not in {"authorization", "x-api-key", "x-api_key"}
        }
        safe_error = error
        if safe_error:
            for secret in secret_values:
                if secret:
                    safe_error = safe_error.replace(secret, "[REDACTED]")
        self.connection.execute(
            "INSERT INTO attempts(document_id, outcome, error, headers_json) VALUES (?, ?, ?, ?)",
            (document_id, outcome, safe_error, json.dumps(safe_headers, sort_keys=True)),
        )
        self.connection.commit()

    def list_attempts(self, document_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT outcome, error, headers_json FROM attempts WHERE document_id = ? ORDER BY id",
            (document_id,),
        ).fetchall()
        return [
            {
                "outcome": row["outcome"],
                "error": row["error"],
                "headers": json.loads(row["headers_json"]),
            }
            for row in rows
        ]

    def list_documents(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT document_id, application_number, status, sha256, path,
                   error_category, payload_json
            FROM documents ORDER BY application_number, document_id
            """
        ).fetchall()
        return [
            {
                "document_id": row["document_id"],
                "application_number": row["application_number"],
                "status": row["status"],
                "sha256": row["sha256"],
                "path": row["path"],
                "error_category": row["error_category"],
                "payload": json.loads(row["payload_json"]),
            }
            for row in rows
        ]


@dataclass(frozen=True)
class StoredFile:
    path: Path
    sha256: str
    byte_length: int
    reused: bool


def _chunks(source: Iterable[bytes] | BinaryIO) -> Iterator[bytes]:
    if hasattr(source, "read"):
        while True:
            chunk = source.read(1024 * 1024)  # type: ignore[union-attr]
            if not chunk:
                break
            yield chunk
    else:
        yield from source


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def store_download(
    source: Iterable[bytes] | BinaryIO,
    destination: Path,
    *,
    expected_size: int | None = None,
) -> StoredFile:
    """Atomically store a byte stream, preserving mismatched prior content."""

    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        descriptor, temp_name = tempfile.mkstemp(
            prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
        )
        temp_path = Path(temp_name)
        digest = hashlib.sha256()
        byte_length = 0
        with os.fdopen(descriptor, "wb") as handle:
            for chunk in _chunks(source):
                if not isinstance(chunk, bytes):
                    raise TypeError("download chunks must be bytes")
                handle.write(chunk)
                digest.update(chunk)
                byte_length += len(chunk)
            handle.flush()
            os.fsync(handle.fileno())
        if expected_size is not None and byte_length != expected_size:
            raise SizeMismatchError(
                f"expected {expected_size} bytes, received {byte_length}"
            )
        sha256 = digest.hexdigest()
        if destination.exists():
            existing_hash = _file_hash(destination)
            if existing_hash == sha256:
                temp_path.unlink()
                temp_path = None
                return StoredFile(destination, sha256, byte_length, True)
            quarantine = destination.with_name(
                f"{destination.name}.quarantine.{existing_hash}"
            )
            if quarantine.exists():
                if _file_hash(quarantine) != existing_hash:
                    raise FileExistsError(f"quarantine collision: {quarantine}")
                destination.unlink()
            else:
                os.replace(destination, quarantine)
        os.replace(temp_path, destination)
        temp_path = None
        return StoredFile(destination, sha256, byte_length, False)
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()


def write_jsonl(path: Path, rows: Iterable[Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(to_json_dict(row), sort_keys=True, ensure_ascii=False))
                handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def read_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    with Path(path).open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                yield json.loads(line)

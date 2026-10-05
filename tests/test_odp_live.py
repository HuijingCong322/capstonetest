"""Opt-in live smoke test for the USPTO ODP adapter."""

import os
from pathlib import Path

import httpx
import pytest

from prosecution_data.odp import OdpClient, OdpSettings


@pytest.mark.live
def test_live_document_metadata_smoke(tmp_path: Path) -> None:
    if os.environ.get("RUN_USPTO_LIVE_TESTS") != "1":
        pytest.skip("set RUN_USPTO_LIVE_TESTS=1 to enable live USPTO tests")
    api_key = os.environ.get("USPTO_API_KEY")
    if not api_key:
        pytest.skip("USPTO_API_KEY is required for the live test")
    application_number = os.environ.get("USPTO_LIVE_APPLICATION", "16123456")

    with httpx.Client() as http_client:
        documents = OdpClient(
            http_client,
            OdpSettings(api_key=api_key, output_root=tmp_path, max_retries=1),
        ).list_documents(application_number)

    assert isinstance(documents, list)
    assert all(record.application_number.isdigit() for record in documents)

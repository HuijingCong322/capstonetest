from pathlib import Path
from io import BytesIO
import json

import httpx
import pandas as pd
import pytest
from reportlab.pdfgen.canvas import Canvas

from prosecution_data.cli import main
from prosecution_data.storage import read_jsonl


PATEX = Path(__file__).parent / "fixtures" / "patex"


def sample_args(output_root: Path) -> list[str]:
    return [
        "--output-root",
        str(output_root),
        "sample",
        "--application-data",
        str(PATEX / "application_data.csv"),
        "--transactions",
        str(PATEX / "transactions.csv"),
        "--cms-documents",
        str(PATEX / "cms_documents.csv"),
        "--cms-document-codes",
        str(PATEX / "cms_document_codes.csv"),
        "--continuity-parents",
        str(PATEX / "continuity_parents.csv"),
        "--sample-size",
        "20",
    ]


def test_help_lists_all_pipeline_commands(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["--help"], {})

    assert exc_info.value.code == 0
    output = capsys.readouterr().out
    for command in ("sample", "fetch", "extract", "build-timeline", "run"):
        assert command in output


def test_config_explicit_output_root_takes_precedence(tmp_path: Path) -> None:
    configured = tmp_path / "configured"
    explicit = tmp_path / "explicit"
    config_path = tmp_path / "pipeline.toml"
    config_path.write_text(f'output_root = "{configured}"\n', encoding="utf-8")

    exit_code = main(
        ["--config", str(config_path), *sample_args(explicit)],
        {},
    )

    assert exit_code == 0
    assert (explicit / "manifests" / "applications.jsonl").exists()
    assert not configured.exists()


def test_credential_is_not_accepted_as_a_command_line_flag() -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["fetch", "--api-key", "secret"], {})

    assert exc_info.value.code == 2


def test_fetch_without_credentials_fails_before_network(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    manifest = tmp_path / "applications.jsonl"
    manifest.write_text('{"application_number":"12000001"}\n', encoding="utf-8")

    exit_code = main(
        [
            "--output-root",
            str(tmp_path / "output"),
            "fetch",
            "--applications-manifest",
            str(manifest),
        ],
        {},
    )

    assert exit_code != 0
    error = capsys.readouterr().err
    assert "USPTO_API_KEY" in error


def test_offline_sample_runs_without_api_key(tmp_path: Path) -> None:
    output_root = tmp_path / "output"

    exit_code = main(sample_args(output_root), {})

    assert exit_code == 0
    applications = list(read_jsonl(output_root / "manifests" / "applications.jsonl"))
    assert [row["application_number"] for row in applications] == [
        "12000001",
        "12000003",
        "12000005",
    ]


def pdf_payload() -> bytes:
    buffer = BytesIO()
    canvas = Canvas(buffer)
    canvas.drawString(
        72,
        720,
        "This patent prosecution document contains enough native text for deterministic extraction.",
    )
    canvas.save()
    return buffer.getvalue()


def test_end_to_end_fixture_run_writes_all_outputs_and_is_rerunnable(tmp_path: Path) -> None:
    output_root = tmp_path / "output"
    payload = pdf_payload()

    def handler(request: httpx.Request) -> httpx.Response:
        parts = request.url.path.strip("/").split("/")
        if request.url.path.endswith("/documents"):
            application_number = parts[-2]
            items = [
                {
                    "applicationNumberText": application_number,
                    "documentIdentifier": f"{application_number}-OA",
                    "documentCode": "CTNF",
                    "officialDate": "2021-01-01",
                    "documentSizeQuantity": len(payload),
                },
                {
                    "applicationNumberText": application_number,
                    "documentIdentifier": f"{application_number}-RESP",
                    "documentCode": "RESP",
                    "officialDate": "2021-02-01",
                    "documentSizeQuantity": len(payload),
                },
            ]
            return httpx.Response(
                200,
                request=request,
                json={"documentBag": items, "count": 2, "totalRecordCount": 2},
            )
        return httpx.Response(200, request=request, content=payload)

    def client_factory() -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(handler))

    assert main(sample_args(output_root), {}) == 0
    applications_manifest = output_root / "manifests" / "applications.jsonl"
    assert main(
        [
            "--output-root",
            str(output_root),
            "fetch",
            "--applications-manifest",
            str(applications_manifest),
        ],
        {"USPTO_API_KEY": "test-secret"},
        http_client_factory=client_factory,
    ) == 0
    assert main(["--output-root", str(output_root), "extract"], {}) == 0
    assert main(["--output-root", str(output_root), "build-timeline"], {}) == 0
    assert main(["--output-root", str(output_root), "extract"], {}) == 0

    timelines = list(read_jsonl(output_root / "processed" / "timelines.jsonl"))
    parquet = pd.read_parquet(output_root / "processed" / "timelines.parquet")
    report_text = (output_root / "reports" / "acquisition_summary.json").read_text(
        encoding="utf-8"
    )
    report = json.loads(report_text)
    odp_manifest = list(read_jsonl(output_root / "manifests" / "odp_documents.jsonl"))

    assert len(timelines) == 3
    assert len(parquet) == 3
    assert (output_root / "state" / "prosecution_data.sqlite3").exists()
    assert len(list((output_root / "interim" / "text").glob("*.json"))) == 6
    assert report["sampling"]["selected_applications"] == 3
    assert report["downloads"]["extracted"] == 6
    assert report["eligibility"]["generation_eligible"] == 3
    assert len(odp_manifest) == 6
    assert all(row["acquisition_status"] == "downloaded" for row in odp_manifest)
    assert all(row["acquired_sha256"] for row in odp_manifest)
    assert all(row["local_path"] for row in odp_manifest)
    assert all(row["acquired_byte_length"] == len(payload) for row in odp_manifest)
    assert all(row["retrieved_at"] for row in odp_manifest)
    assert "test-secret" not in report_text

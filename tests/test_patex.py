from pathlib import Path
from datetime import date

import pytest

from prosecution_data.patex import PatExValidationError, build_manifests, validate_patex_inputs
from prosecution_data.schemas import PatExInputs, SampleOptions


FIXTURES = Path(__file__).parent / "fixtures" / "patex"


def fixture_inputs(**overrides: Path | None) -> PatExInputs:
    values: dict[str, Path | None] = {
        "application_data": FIXTURES / "application_data.csv",
        "transactions": FIXTURES / "transactions.csv",
        "cms_documents": FIXTURES / "cms_documents.csv",
        "cms_document_codes": FIXTURES / "cms_document_codes.csv",
        "continuity_parents": FIXTURES / "continuity_parents.csv",
        "continuity_children": None,
    }
    values.update(overrides)
    return PatExInputs(**values)  # type: ignore[arg-type]


def test_validation_accepts_documented_aliases_and_extra_columns() -> None:
    validate_patex_inputs(fixture_inputs())


def test_validation_names_a_missing_required_file(tmp_path: Path) -> None:
    missing = tmp_path / "transactions.csv"

    with pytest.raises(PatExValidationError, match=r"transactions\.csv.*does not exist"):
        validate_patex_inputs(fixture_inputs(transactions=missing))


def test_validation_names_file_and_missing_canonical_columns(tmp_path: Path) -> None:
    bad = tmp_path / "cms_documents.csv"
    bad.write_text("appl_id,document_identifier\n12/000001,DOC-1\n", encoding="utf-8")

    with pytest.raises(PatExValidationError) as exc_info:
        validate_patex_inputs(fixture_inputs(cms_documents=bad))

    message = str(exc_info.value)
    assert "cms_documents.csv" in message
    assert "document_code" in message
    assert "document_date" in message


def test_sample_keeps_all_outcomes_and_assigns_task_eligibility() -> None:
    bundle = build_manifests(
        fixture_inputs(),
        SampleOptions(sample_size=20, random_seed=7, one_per_family=False, chunksize=2),
    )

    by_number = {record.application_number: record for record in bundle.applications}
    assert set(by_number) == {"12000001", "12000002", "12000003", "12000005", "12000006"}
    assert {record.status for record in bundle.applications} == {
        "allowed",
        "abandoned",
        "pending",
    }
    assert by_number["12000001"].generation_eligible is True
    assert by_number["12000002"].generation_eligible is False
    assert all(record.forecast_eligible for record in bundle.applications)


def test_family_sampling_selects_one_stable_representative() -> None:
    options = SampleOptions(sample_size=20, random_seed=99, one_per_family=True, chunksize=2)

    first = build_manifests(fixture_inputs(), options)
    second = build_manifests(fixture_inputs(), options)

    numbers = [record.application_number for record in first.applications]
    assert numbers == ["12000001", "12000003", "12000005"]
    assert first == second
    assert first.applications[0].family_id == "12000001"
    assert first.applications[0].family_resolution == "resolved"


def test_sample_size_and_seed_are_deterministic() -> None:
    options = SampleOptions(sample_size=2, random_seed=3, one_per_family=False, chunksize=2)

    first = build_manifests(fixture_inputs(), options)
    second = build_manifests(fixture_inputs(), options)

    assert [item.application_number for item in first.applications] == [
        item.application_number for item in second.applications
    ]
    assert len(first.applications) == 2


def test_date_bounds_apply_to_application_filing_date() -> None:
    bundle = build_manifests(
        fixture_inputs(),
        SampleOptions(
            sample_size=20,
            date_from=date(2019, 1, 1),
            date_to=date(2019, 12, 31),
            one_per_family=False,
        ),
    )

    assert [record.application_number for record in bundle.applications] == ["12000003"]


def test_missing_continuity_tables_use_unavailable_singletons() -> None:
    bundle = build_manifests(
        fixture_inputs(continuity_parents=None, continuity_children=None),
        SampleOptions(sample_size=20, one_per_family=True),
    )

    assert len(bundle.applications) == 5
    assert all(record.family_resolution == "unavailable" for record in bundle.applications)
    assert all(record.family_id == record.application_number for record in bundle.applications)


def test_unknown_document_codes_are_preserved_and_counted() -> None:
    bundle = build_manifests(
        fixture_inputs(),
        SampleOptions(sample_size=20, one_per_family=False),
    )

    unknown = next(record for record in bundle.documents if record.document_id == "DOC-5")
    assert unknown.document_category == "other"
    assert bundle.counters["unknown_document_codes"] == 1


def test_invalid_identifier_row_is_rejected_and_counted(tmp_path: Path) -> None:
    application_data = tmp_path / "application_data.csv"
    application_data.write_text(
        (FIXTURES / "application_data.csv").read_text(encoding="utf-8")
        + "1.2000007E7,2021-01-01,Application Undergoing Examination,Y,ignored\n",
        encoding="utf-8",
    )

    bundle = build_manifests(
        fixture_inputs(application_data=application_data),
        SampleOptions(sample_size=20, one_per_family=False),
    )

    assert bundle.counters["invalid_application_identifiers"] == 1
    assert all(record.application_number != "12000007" for record in bundle.applications)

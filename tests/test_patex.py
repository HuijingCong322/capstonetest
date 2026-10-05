from pathlib import Path
from datetime import date

import pandas as pd
import pytest

from prosecution_data.patex import (
    PatExValidationError,
    build_manifests,
    load_patex_mapping,
    validate_patex_inputs,
)
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

    unknown = next(
        record for record in bundle.documents
        if record.metadata["source_document_id"] == "DOC-5"
    )
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


def test_official_2022_headers_are_accepted_and_receive_synthetic_ids(tmp_path: Path) -> None:
    application_data = tmp_path / "application_data.csv"
    application_data.write_text(
        "application_number,filing_date,appl_status_desc,public_indicator\n"
        "12/100001,2020-01-01,Application Undergoing Examination,Y\n",
        encoding="utf-8",
    )
    transactions = tmp_path / "transactions.csv"
    transactions.write_text(
        "application_number,event_code,recorded_date\n12/100001,A...,2021-02-01\n",
        encoding="utf-8",
    )
    cms_documents = tmp_path / "cms_documents.csv"
    cms_documents.write_text(
        "application_number,mailroom_date,document_code,number_of_pages\n"
        "12/100001,2021-01-01,CTNF,8\n",
        encoding="utf-8",
    )
    cms_codes = tmp_path / "cms_document_codes.csv"
    cms_codes.write_text(
        "document_code,document_description\nCTNF,Non-Final Rejection\n",
        encoding="utf-8",
    )
    inputs = PatExInputs(application_data, transactions, cms_documents, cms_codes)

    validate_patex_inputs(inputs)
    bundle = build_manifests(inputs, SampleOptions(sample_size=10))

    assert len(bundle.applications) == 1
    assert bundle.documents[0].document_id.startswith("patex-doc:")
    assert bundle.documents[0].metadata["identifier_kind"] == "synthetic_patex_event"
    assert bundle.transactions[0].source_identifier.startswith("patex-tx:")
    assert bundle.transactions[0].description is None


def test_ingestion_filters_chunks_without_concatenating_full_tables(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pd, "concat", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("concat")))

    bundle = build_manifests(
        fixture_inputs(), SampleOptions(sample_size=2, one_per_family=False, chunksize=1)
    )

    assert len(bundle.applications) == 2


def test_family_reconstruction_keeps_siblings_connected_through_excluded_parent(tmp_path: Path) -> None:
    continuity = tmp_path / "continuity_parents.csv"
    continuity.write_text(
        "appl_id,parent_appl_id\n12/000001,12/999999\n12/000002,12/999999\n",
        encoding="utf-8",
    )

    bundle = build_manifests(
        fixture_inputs(continuity_parents=continuity),
        SampleOptions(sample_size=20, one_per_family=True),
    )

    numbers = [record.application_number for record in bundle.applications]
    assert "12000001" in numbers
    assert "12000002" not in numbers


def test_transaction_identifiers_are_unique_and_content_stable() -> None:
    bundle = build_manifests(
        fixture_inputs(), SampleOptions(sample_size=20, one_per_family=False)
    )
    identifiers = [record.source_identifier for record in bundle.transactions]

    assert len(identifiers) == len(set(identifiers))
    assert all(identifier.startswith("patex-tx:") for identifier in identifiers)


def test_transaction_order_and_ids_are_stable_when_source_rows_are_reordered(
    tmp_path: Path,
) -> None:
    header, *rows = (FIXTURES / "transactions.csv").read_text(encoding="utf-8").splitlines()
    rows.extend(
        [
            "12/000001,RESP,2019-03-01,Second same-day response",
            "12/000001,RESP,,Undated response",
        ]
    )
    forward = tmp_path / "forward.csv"
    reverse = tmp_path / "reverse.csv"
    forward.write_text("\n".join([header, *rows]) + "\n", encoding="utf-8")
    reverse.write_text("\n".join([header, *reversed(rows)]) + "\n", encoding="utf-8")
    options = SampleOptions(sample_size=20, one_per_family=False, chunksize=2)

    first = build_manifests(fixture_inputs(transactions=forward), options)
    second = build_manifests(fixture_inputs(transactions=reverse), options)

    assert first.transactions == second.transactions


def test_official_response_and_allowance_codes_are_categorized() -> None:
    categories = load_patex_mapping()["transaction_categories"]

    assert "A..." in categories["applicant_response"]
    assert "N/=" in categories["next_examination_event"]
    assert "MN/=" in categories["next_examination_event"]


def test_real_release_metadata_without_cms_or_public_indicator(tmp_path):
    app = tmp_path / 'application_data.csv'
    app.write_text('application_number,filing_date,appl_status_desc,earliest_pgpub_number,patent_number\n01234567,2010-01-01,Abandoned,US20100123456,\n01234568,2010-01-02,Pending,,\n')
    tx = tmp_path / 'transactions.csv'
    tx.write_text('application_number,event_code,recorded_date\n01234567,MCTNF,2011-01-01\n01234567,A...,2011-02-01\n01234567,MN/=.,2011-03-01\n01234568,MCTNF,2011-01-01\n')
    codes = tmp_path / 'event_codes.csv'
    codes.write_text('event_code,description\nMCTNF,Mail Non-Final Rejection\nA...,Response after Non-Final Action\nMN/=.,Mail Notice of Allowance\n')
    inputs = PatExInputs(application_data=app, transactions=tx, event_codes=codes)
    bundle = build_manifests(inputs, SampleOptions(sample_size=3, one_per_family=False))
    assert [a.application_number for a in bundle.applications] == ['01234567']
    assert bundle.documents == ()
    assert bundle.transactions[0].description == 'Mail Non-Final Rejection'
    assert bundle.applications[0].generation_eligible is False
    assert bundle.applications[0].metadata['response_observed'] is True


def test_real_transaction_codes_preserve_punctuation():
    categories = load_patex_mapping()['transaction_categories']
    assert 'MCTNF' in categories['office_action']
    assert 'MCTFR' in categories['office_action']
    assert 'MN/=.' in categories['next_examination_event']
    assert 'A/RR' in categories['applicant_response']


def test_timeline_stops_at_procedure_change():
    from prosecution_data.timeline import build_timeline, TimelineSettings
    from prosecution_data.schemas import ApplicationRecord, TransactionEvent
    app = ApplicationRecord('01234567', date(2010,1,1), 'pending', '01234567', 'singleton', False, True)
    tx = [TransactionEvent('01234567', code, date(2011,month,1), source_identifier=code)
          for month,code in enumerate(['MCTNF','A...','RCEX','MCTFR'],1)]
    timeline = build_timeline(app, [], tx, [], TimelineSettings())
    assert timeline.candidate_links[0]['next_event_id'] == 'RCEX'

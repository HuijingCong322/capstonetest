from dataclasses import dataclass
from datetime import date

import pytest

from prosecution_data.schemas import (
    ApplicationRecord,
    IdentifierError,
    normalize_application_number,
    to_json_dict,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("12/345,678", "12345678"),
        ("12-345678", "12345678"),
        ("01/234,567", "01234567"),
        (" 12345678 ", "12345678"),
    ],
)
def test_normalize_application_number_accepts_unambiguous_strings(raw: str, expected: str) -> None:
    assert normalize_application_number(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "1.2345678E7", 12345678.0, "12/34A,678", "../12345678"])
def test_normalize_application_number_rejects_ambiguous_or_unsafe_values(raw: object) -> None:
    with pytest.raises(IdentifierError):
        normalize_application_number(raw)


def test_application_record_serializes_dates_and_nested_dataclasses() -> None:
    @dataclass(frozen=True)
    class Nested:
        label: str

    record = ApplicationRecord(
        application_number="12345678",
        filing_date=date(2020, 1, 2),
        status="pending",
        family_id="12345678",
        family_resolution="unavailable",
        generation_eligible=False,
        forecast_eligible=True,
        metadata={"nested": Nested("value")},
    )

    assert to_json_dict(record) == {
        "application_number": "12345678",
        "filing_date": "2020-01-02",
        "status": "pending",
        "family_id": "12345678",
        "family_resolution": "unavailable",
        "generation_eligible": False,
        "forecast_eligible": True,
        "metadata": {"nested": {"label": "value"}},
    }

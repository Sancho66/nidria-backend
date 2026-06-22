"""Per-cell validation (BLOC 1) — the 3 target families, OK + KO each.

Pure unit tests: no DB, no HTTP. The CustomFieldDefinition is built
in-memory (never persisted) — `_coerce_one` only reads its attributes.
"""

from shared.models.custom_field import CustomFieldDefinition
from src.core.enums import CustomFieldType
from src.imports.cell_validation import (
    BaseFieldTarget,
    CaseFieldTarget,
    CustomFieldTarget,
    validate_cell,
)

# --- base_field ----------------------------------------------------------------------


def test_base_sex_ok_normalizes() -> None:
    result = validate_cell("Sex", BaseFieldTarget("sex"), "f")
    assert result.ok
    assert result.value == "F"


def test_base_sex_ko_reports_error() -> None:
    result = validate_cell("Sex", BaseFieldTarget("sex"), "Z")
    assert not result.ok
    assert result.error is not None
    assert result.error.column == "Sex"
    assert "one of" in result.error.reason


def test_base_marital_status_ok() -> None:
    result = validate_cell("Statut", BaseFieldTarget("marital_status"), "Married")
    assert result.ok
    assert result.value == "married"


def test_base_date_of_birth_ok() -> None:
    result = validate_cell("DOB", BaseFieldTarget("date_of_birth"), "1990-01-02")
    assert result.ok
    assert result.value == "1990-01-02"


def test_base_date_of_birth_ko_malformed() -> None:
    result = validate_cell("DOB", BaseFieldTarget("date_of_birth"), "31/12/1990")
    assert not result.ok
    assert result.error is not None
    assert "date" in result.error.reason


# --- case_field ----------------------------------------------------------------------


def test_case_country_ok() -> None:
    result = validate_cell("Country", CaseFieldTarget("dest_country"), "PY")
    assert result.ok
    assert result.value == "PY"


def test_case_country_ko_non_iso() -> None:
    result = validate_cell("Country", CaseFieldTarget("dest_country"), "FRA")
    assert not result.ok
    assert result.error is not None
    assert "ISO" in result.error.reason


def test_case_street_length_capped() -> None:
    result = validate_cell("Street", CaseFieldTarget("origin_street"), "x" * 256)
    assert not result.ok
    assert result.error is not None
    assert "too long" in result.error.reason


# --- custom_field --------------------------------------------------------------------


def _select_def() -> CustomFieldDefinition:
    return CustomFieldDefinition(
        key="visa_type",
        label="Visa type",
        field_type=CustomFieldType.SELECT.value,
        options=["tourist", "business"],
    )


def test_custom_select_ok() -> None:
    result = validate_cell("Visa", CustomFieldTarget(_select_def()), "tourist")
    assert result.ok
    assert result.value == "tourist"


def test_custom_select_ko_out_of_options() -> None:
    result = validate_cell("Visa", CustomFieldTarget(_select_def()), "student")
    assert not result.ok
    assert result.error is not None
    assert "one of" in result.error.reason


# --- empty cells ---------------------------------------------------------------------


def test_empty_cell_is_ok_with_none() -> None:
    result = validate_cell("Sex", BaseFieldTarget("sex"), "   ")
    assert result.ok
    assert result.value is None

"""Per-cell value validation for the import socle (BLOC 1).

Validates ONE raw CSV string against the type of its TARGET field, across
the three families a journey's Informations tab can declare:

- base_field   → a civil-status column on `case_person`
                 (sex, marital_status, date_of_birth, nationality, …);
- case_field   → an address column on `client_case`
                 (origin/dest country/street/city/postal_code);
- custom_field → an agency-defined `CustomFieldDefinition`
                 (text/number/date/boolean/select/multi_select/country/address).

REUSES `custom_fields_validation` for every shared rule (date, ISO-2
country, the whole custom-field coercion) — no duplicated regex or parser.

Contract: a function NEVER raises on a bad cell. It returns a `CellResult`
carrying either the coerced value or a typed `CellError {column, reason}`.
An empty cell is OK with value None ("not provided") — required-ness is a
mapping-time concern (BLOC 2/3), not a per-cell rule.
"""

from dataclasses import dataclass
from typing import Any

from shared.models.custom_field import CustomFieldDefinition
from src.cases.case_fields import COLLECTABLE_CASE_FIELDS
from src.core.enums import MaritalStatus, Sex
from src.custom_fields.custom_fields_validation import (
    _coerce_country,
    _coerce_date,
    _coerce_one,
    coerce_address_subfield,
)
from src.progress.requirements_eval import COLLECTABLE_BASE_FIELDS

# Max lengths mirror the model columns (the single source of truth):
#   base text  → shared/models/case_person.py (civil-status columns)
#   case text  → shared/models/client_case.py (address columns)
# Same convention as custom_fields_validation's address sub-field caps.
_BASE_TEXT_MAXLEN: dict[str, int] = {
    "passport_number": 50,
    "nationality": 100,
    "place_of_birth": 200,
    "phone": 50,
    "birth_name": 200,
    "profession": 200,
    "employer": 200,
}
_CASE_TEXT_MAXLEN: dict[str, int] = {
    "origin_street": 255,
    "origin_city": 100,
    "origin_postal_code": 20,
    "dest_street": 255,
    "dest_city": 100,
    "dest_postal_code": 20,
}
_SEX_VALUES = {member.value for member in Sex}
_MARITAL_VALUES = {member.value for member in MaritalStatus}


@dataclass(frozen=True)
class BaseFieldTarget:
    """A civil-status field on case_person, by its reference name."""

    reference: str


@dataclass(frozen=True)
class CaseFieldTarget:
    """An address field on client_case, by its column name."""

    reference: str


@dataclass(frozen=True)
class CustomFieldTarget:
    """An agency-defined custom field, by its active definition."""

    definition: CustomFieldDefinition


@dataclass(frozen=True)
class AddressSubfieldTarget:
    """ONE sub-component (street/city/postal_code/country) of an ADDRESS custom
    field, fed by its OWN CSV column (composite mapping: N columns → one address
    object). The cell carries a sub-field string; the engine assembles the
    object and stores it under the field's key."""

    definition: CustomFieldDefinition
    subfield: str


CellTarget = BaseFieldTarget | CaseFieldTarget | CustomFieldTarget | AddressSubfieldTarget


@dataclass(frozen=True)
class CellError:
    column: str
    reason: str


@dataclass(frozen=True)
class CellResult:
    column: str
    value: Any  # coerced value when ok; None when empty or on error
    error: CellError | None

    @property
    def ok(self) -> bool:
        return self.error is None


def _validate_base(reference: str, raw: str) -> Any:
    if reference not in COLLECTABLE_BASE_FIELDS:
        raise ValueError(f"unknown base field {reference!r}")
    if reference == "sex":
        value = raw.strip().upper()
        if value not in _SEX_VALUES:
            raise ValueError(f"must be one of {sorted(_SEX_VALUES)}")
        return value
    if reference == "marital_status":
        value = raw.strip().lower()
        if value not in _MARITAL_VALUES:
            raise ValueError(f"must be one of {sorted(_MARITAL_VALUES)}")
        return value
    if reference == "date_of_birth":
        return _coerce_date(raw)  # → ISO YYYY-MM-DD, shared rule
    text = raw.strip()
    maxlen = _BASE_TEXT_MAXLEN[reference]
    if len(text) > maxlen:
        raise ValueError(f"too long (max {maxlen} characters)")
    return text


def _validate_case(reference: str, raw: str) -> Any:
    if reference not in COLLECTABLE_CASE_FIELDS:
        raise ValueError(f"unknown case field {reference!r}")
    if reference.endswith("_country"):
        return _coerce_country(raw)  # ISO-2, the shared rule
    text = raw.strip()
    maxlen = _CASE_TEXT_MAXLEN[reference]
    if len(text) > maxlen:
        raise ValueError(f"too long (max {maxlen} characters)")
    return text


def validate_cell(column: str, target: CellTarget, raw: str) -> CellResult:
    """Validate one raw CSV cell against its target field. Empty → ok with
    value None. Never raises: any failure becomes a CellResult with a
    CellError carrying the column and a human reason."""
    if raw.strip() == "":
        return CellResult(column=column, value=None, error=None)
    try:
        if isinstance(target, BaseFieldTarget):
            value = _validate_base(target.reference, raw)
        elif isinstance(target, CaseFieldTarget):
            value = _validate_case(target.reference, raw)
        elif isinstance(target, AddressSubfieldTarget):
            value = coerce_address_subfield(target.subfield, raw)  # one sub-field, shared rule
        else:
            value = _coerce_one(target.definition, raw)  # full custom-field reuse
    except Exception as exc:  # a single bad cell is reported, never fatal
        return CellResult(
            column=column, value=None, error=CellError(column=column, reason=str(exc))
        )
    return CellResult(column=column, value=value, error=None)

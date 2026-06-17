"""Requirement evaluation (read-only) — the SINGLE source of truth for
"is this requirement provided?" and "what is its live value?".

A base_field / custom_field requirement is `provided` iff the real value
is non-empty AT THE BACKING STORE — read live, NEVER copied into a
concrete row. A document requirement carries an explicit status.

Two BACKING PLANES (sections chantier, vague C):
- PERSON: the value lives on `case_person` (the 7 civil columns or the
  custom_fields JSONB) — the historical plane.
- CASE:   the value lives on `client_case` (country / address columns) —
  the case-level plane.

The decision "provided?/value?" is factored behind `resolve_provided` /
`resolve_value`, which dispatch at the LEAF on the plane (which column to
read). `is_provided` / `current_value` (person, + document) are now thin
façades over the resolver — so person behavior is provably unchanged.
"""

from enum import StrEnum
from typing import Any

from shared.models.case_person import CasePerson
from shared.models.case_step_requirement import CaseStepRequirement
from shared.models.client_case import ClientCase
from src.core.enums import RequirementStatus, StepRequirementKind

# Collectable base fields on case_person — the closed whitelist. NEVER
# email/name (those live on the shared expat_user). Mirrors the civil/
# professional status columns (residence_permit_number was removed). A new
# entry here needs a matching nullable column on CasePerson; the resolver
# reads it generically via getattr — no resolver change (vague B).
COLLECTABLE_BASE_FIELDS: frozenset[str] = frozenset(
    {
        "passport_number",
        "date_of_birth",
        "nationality",
        "place_of_birth",
        "sex",
        "marital_status",
        "phone",
        "birth_name",
        "profession",
        "employer",
    }
)


class FieldPlane(StrEnum):
    """Which backing store a field requirement reads/writes."""

    PERSON = "person"
    CASE = "case"


def _is_empty(value: Any) -> bool:
    return value is None or value == "" or value == []


def _person_field_value(person: CasePerson, reference: str) -> Any:
    """PERSON leaf: a base field → a column on case_person; a custom field
    → a key in the JSONB. Read live, nothing copied."""
    if reference in COLLECTABLE_BASE_FIELDS:
        return getattr(person, reference, None)
    return (person.custom_fields or {}).get(reference)


# Backwards-compatible alias (kept so any external import keeps working).
_field_value = _person_field_value


def _case_field_value(case: ClientCase, case_field: str) -> Any:
    """CASE leaf: a column on client_case (country/address). The value
    NEVER leaves client_case — read live, the same column the query
    ecosystem (filters/sorts/KPI/views) reads."""
    return getattr(case, case_field, None)


# --- the factored resolver (dispatch at the leaf on the plane) -----------------------


def resolve_value(
    plane: FieldPlane,
    reference: str,
    *,
    person: CasePerson | None = None,
    case: ClientCase | None = None,
) -> Any:
    """The live value backing a base/custom field requirement, read at
    the source — the SINGLE resolution, dispatched at the leaf. None when
    empty/missing. (Documents are handled by the façade, not here.)"""
    if plane is FieldPlane.PERSON:
        raw = _person_field_value(person, reference) if person is not None else None
    else:
        raw = _case_field_value(case, reference) if case is not None else None
    return None if _is_empty(raw) else raw


def resolve_provided(
    plane: FieldPlane,
    reference: str,
    *,
    person: CasePerson | None = None,
    case: ClientCase | None = None,
) -> bool:
    """Is the field non-empty at its backing store — the SINGLE
    provided-decision, dispatched at the leaf."""
    if plane is FieldPlane.PERSON:
        if person is None:
            return False
        raw = _person_field_value(person, reference)
    else:
        if case is None:
            return False
        raw = _case_field_value(case, reference)
    return not _is_empty(raw)


# --- person façades (unchanged behavior; delegate to the resolver) -------------------


def current_value(requirement: CaseStepRequirement, person: CasePerson | None) -> Any:
    """PERSON façade: live value for a base/custom requirement; None for a
    document (the artifact is the document) and when pending."""
    if requirement.kind == StepRequirementKind.DOCUMENT.value:
        return None
    return resolve_value(FieldPlane.PERSON, requirement.reference, person=person)


def field_provided(person: CasePerson, reference: str) -> bool:
    """Is a base/custom field non-empty on this person — exposed for the
    required-at-creation check (which works from a template field, not a
    CaseStepRequirement)."""
    return resolve_provided(FieldPlane.PERSON, reference, person=person)


def is_provided(requirement: CaseStepRequirement, person: CasePerson | None) -> bool:
    """PERSON façade: base/custom → derived from the live person value;
    document → the explicit stored status."""
    if requirement.kind == StepRequirementKind.DOCUMENT.value:
        return requirement.status == RequirementStatus.PROVIDED.value
    return resolve_provided(FieldPlane.PERSON, requirement.reference, person=person)


# --- case-level helpers (the CASE plane twins; never documents) ----------------------


def case_is_provided(case_requirement: Any, case: ClientCase) -> bool:
    """Is a case-level requirement's client_case column non-empty."""
    return resolve_provided(FieldPlane.CASE, case_requirement.case_field, case=case)


def case_current_value(case_requirement: Any, case: ClientCase) -> Any:
    """The live value of a case-level requirement, read on client_case."""
    return resolve_value(FieldPlane.CASE, case_requirement.case_field, case=case)

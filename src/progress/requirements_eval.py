"""Requirement evaluation (NEW WAVE, read-only).

Single source of truth: a base_field / custom_field requirement is
`provided` iff the real value is non-empty ON THE PERSON — read live
from case_person, NEVER copied into case_step_requirement. A document
requirement carries an explicit status (the document is the artifact).
"""

from typing import Any

from shared.models.case_person import CasePerson
from shared.models.case_step_requirement import CaseStepRequirement
from src.core.enums import RequirementStatus, StepRequirementKind

# Collectable base fields on case_person — the closed whitelist. NEVER
# email/name (those live on the shared expat_user). Mirrors the civil
# status columns (residence_permit_number was removed).
COLLECTABLE_BASE_FIELDS: frozenset[str] = frozenset(
    {
        "passport_number",
        "date_of_birth",
        "nationality",
        "place_of_birth",
        "sex",
        "marital_status",
        "phone",
    }
)


def _is_empty(value: Any) -> bool:
    return value is None or value == "" or value == []


def _field_value(person: CasePerson, reference: str) -> Any:
    """Base field → a column on case_person; custom field → a key in
    the JSONB. Both read live; nothing is copied."""
    if reference in COLLECTABLE_BASE_FIELDS:
        return getattr(person, reference, None)
    return (person.custom_fields or {}).get(reference)


def is_provided(requirement: CaseStepRequirement, person: CasePerson | None) -> bool:
    """base_field / custom_field → derived from the live person value;
    document → the explicit stored status. A missing person (should not
    happen — materialized persons CASCADE with the case) reads as not
    provided rather than raising."""
    if requirement.kind == StepRequirementKind.DOCUMENT.value:
        return requirement.status == RequirementStatus.PROVIDED.value
    if person is None:
        return False
    return not _is_empty(_field_value(person, requirement.reference))

"""Routing of EXPAT reminders to the PERSON concerned (2026-07-18, the
multi-person limit closed — promise to Nicolas).

The rule: a reminder whose step's PENDING requirements ALL target ONE
precise person goes to that person when she has an ACTIVE ACCESS
(case_person.expat_user_id + an email); otherwise — no step, no pending
requirement, several persons, member without access — the PRINCIPAL, as
before. Never both. The pure core lives here so the sync dispatch job
and the async approval-screen resolution share ONE truth."""

import uuid

from shared.models.case_person import CasePerson
from shared.models.case_step_requirement import CaseStepRequirement
from src.core.enums import CasePersonKind
from src.progress.requirements_eval import is_provided


def pending_target_person_id(
    requirements: list[CaseStepRequirement], persons: dict[uuid.UUID, CasePerson]
) -> uuid.UUID | None:
    """The ONE person all pending requirements of the step point at —
    None when there is nothing pending or several persons are concerned
    (the principal then remains the voice of the dossier)."""
    pending = [r for r in requirements if not is_provided(r, persons.get(r.person_id))]
    person_ids = {r.person_id for r in pending}
    if len(person_ids) == 1:
        return next(iter(person_ids))
    return None


def targeted_member(
    requirements: list[CaseStepRequirement], persons: dict[uuid.UUID, CasePerson]
) -> CasePerson | None:
    """The targeted person IF she is a MEMBER with an account link —
    the principal target falls back to None (the principal path already
    serves them), so callers route member-or-principal, never both."""
    target_id = pending_target_person_id(requirements, persons)
    if target_id is None:
        return None
    person = persons.get(target_id)
    if (
        person is None
        or person.kind == CasePersonKind.PRINCIPAL.value
        or person.expat_user_id is None
    ):
        return None
    return person

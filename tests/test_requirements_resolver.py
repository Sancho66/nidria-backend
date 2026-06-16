"""Factored resolver (sections chantier, vague C) — proof that the PERSON
plane behaves EXACTLY as before the refactor, and that the CASE plane
reads client_case. Pure unit test (no DB): the resolver is getattr/JSONB
+ emptiness, the façades delegate to it.

Renforcement 1: not "it compiles" — the proof that `is_provided` /
`current_value` (now façades over `resolve_*`) return the documented
person semantics on a base/custom/document sample, AND that they are
literally the resolver's leaf for field kinds."""

from types import SimpleNamespace

from shared.models.case_person import CasePerson
from shared.models.client_case import ClientCase
from src.core.enums import RequirementStatus
from src.progress.requirements_eval import (
    FieldPlane,
    case_current_value,
    case_is_provided,
    current_value,
    is_provided,
    resolve_provided,
    resolve_value,
)


def _person() -> CasePerson:
    return CasePerson(
        kind="principal",
        passport_number="AB123",  # base field, filled
        nationality=None,  # base field, empty
        custom_fields={"visa_type": "work"},  # custom field, filled
    )


def _req(kind: str, reference: str, status: str = "pending") -> SimpleNamespace:
    return SimpleNamespace(kind=kind, reference=reference, status=status)


# --- PERSON plane: behavior unchanged (the proof) ------------------------------------


def test_person_is_provided_unchanged() -> None:
    person = _person()
    assert is_provided(_req("base_field", "passport_number"), person) is True
    assert is_provided(_req("base_field", "nationality"), person) is False
    assert is_provided(_req("custom_field", "visa_type"), person) is True
    assert is_provided(_req("custom_field", "ghost"), person) is False
    # document → explicit status, NOT a backing read.
    assert is_provided(_req("document", "x", RequirementStatus.PROVIDED.value), person) is True
    assert is_provided(_req("document", "x", "pending"), person) is False
    # missing person → not provided.
    assert is_provided(_req("base_field", "passport_number"), None) is False


def test_person_current_value_unchanged() -> None:
    person = _person()
    assert current_value(_req("base_field", "passport_number"), person) == "AB123"
    assert current_value(_req("base_field", "nationality"), person) is None
    assert current_value(_req("custom_field", "visa_type"), person) == "work"
    # document → never a value (the artifact is the document).
    assert current_value(_req("document", "x", RequirementStatus.PROVIDED.value), person) is None


def test_facade_equals_resolver_leaf_for_fields() -> None:
    """The façade IS the resolver for field kinds — no divergent path."""
    person = _person()
    for ref in ("passport_number", "nationality", "visa_type"):
        req = _req("base_field" if ref != "visa_type" else "custom_field", ref)
        assert is_provided(req, person) == resolve_provided(FieldPlane.PERSON, ref, person=person)
        assert current_value(req, person) == resolve_value(FieldPlane.PERSON, ref, person=person)


# --- CASE plane: reads client_case ---------------------------------------------------


def test_case_plane_reads_client_case() -> None:
    case = ClientCase(origin_country="FR", origin_city="Paris", dest_country=None)
    assert case_is_provided(SimpleNamespace(case_field="origin_country"), case) is True
    assert case_is_provided(SimpleNamespace(case_field="origin_city"), case) is True
    assert case_is_provided(SimpleNamespace(case_field="dest_country"), case) is False
    assert case_current_value(SimpleNamespace(case_field="origin_country"), case) == "FR"
    assert case_current_value(SimpleNamespace(case_field="dest_country"), case) is None
    # Same single decision, just the other leaf.
    assert resolve_value(FieldPlane.CASE, "origin_city", case=case) == "Paris"
    assert resolve_provided(FieldPlane.CASE, "dest_country", case=case) is False


def test_resolver_missing_context_is_not_provided() -> None:
    assert resolve_provided(FieldPlane.PERSON, "passport_number", person=None) is False
    assert resolve_provided(FieldPlane.CASE, "origin_country", case=None) is False
    assert resolve_value(FieldPlane.PERSON, "passport_number", person=None) is None
    assert resolve_value(FieldPlane.CASE, "origin_country", case=None) is None

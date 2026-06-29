"""Composite mapping → one ADDRESS field (N CSV columns → 1 address object).

Two layers, no rule duplicated:
- PURE (no DB): the token grammar (`custom_field:<key>.<subfield>`), the
  per-sub-cell validation (reuses the V1 ISO-2 / length rules), and the
  parcours/dedup/conflict checks of `validate_mapping_targets`.
- INTEGRATION (DB + HTTP): the engine assembling the object from 4 columns,
  AND a partial address (only City + Country) — neither is an error.
"""

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from shared.models.case_person import CasePerson
from shared.models.custom_field import CustomFieldDefinition
from shared.models.journey import JourneyTemplate, JourneyTemplateField
from shared.models.rbac import Role
from src.core.enums import CustomFieldType
from src.core.exceptions import ValidationError
from src.imports.case_import_repository import DeclaredField
from src.imports.cell_validation import AddressSubfieldTarget, validate_cell
from src.imports.mapping_validation import MappingTarget, parse_token, validate_mapping_targets
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent
from tests.plugins.journey_plugin import MakeJourneyTemplate, MakeTemplateStep

# ============================ PURE — token grammar ============================


def test_parse_composite_token_splits_key_and_subfield() -> None:
    target = parse_token("custom_field:adresse.street")
    assert target == MappingTarget("custom_field", "adresse", subpath="street")
    # round-trips back to the source token (used in error messages)
    assert target.token() == "custom_field:adresse.street"


def test_parse_simple_custom_token_has_no_subpath() -> None:
    # A whole-object custom field is unchanged — no regression on simple tokens.
    assert parse_token("custom_field:adresse") == MappingTarget("custom_field", "adresse")
    assert parse_token("base_field:nationality") == MappingTarget("base_field", "nationality")
    assert parse_token("email") == MappingTarget("identity", "email")


def test_parse_unknown_subfield_is_unparseable() -> None:
    # Only the four known address components are accepted as a sub-path.
    assert parse_token("custom_field:adresse.region") is None
    assert parse_token("custom_field:adresse.") is None


def test_parse_subpath_only_on_custom_field() -> None:
    # base/case fields carry no structured object → a dotted reference is just a
    # (non-declared) reference, never a sub-path.
    assert parse_token("case_field:origin.street") == MappingTarget("case_field", "origin.street")


# ====================== PURE — per-sub-cell validation =======================


def _addr_def(field_type: str = CustomFieldType.ADDRESS.value) -> CustomFieldDefinition:
    return CustomFieldDefinition(key="adresse", label="Adresse", field_type=field_type)


def test_address_subfield_country_reuses_iso2_rule() -> None:
    ok = validate_cell("Country", AddressSubfieldTarget(_addr_def(), "country"), "fr")
    # ISO-2 rule rejects lowercase → reported, never raised
    assert not ok.ok
    good = validate_cell("Country", AddressSubfieldTarget(_addr_def(), "country"), "FR")
    assert good.ok and good.value == "FR"


def test_address_subfield_string_length_capped() -> None:
    res = validate_cell("Street", AddressSubfieldTarget(_addr_def(), "street"), "x" * 256)
    assert not res.ok
    assert res.error is not None and "too long" in res.error.reason


def test_address_subfield_empty_is_not_provided() -> None:
    res = validate_cell("City", AddressSubfieldTarget(_addr_def(), "city"), "   ")
    assert res.ok and res.value is None


# ==================== PURE — validate_mapping_targets ========================

_DECLARED = [DeclaredField(family="custom_field", reference="adresse", required=False)]
_DEFS = {"adresse": _addr_def()}
_COMPOSITE = {
    "Street": "custom_field:adresse.street",
    "City": "custom_field:adresse.city",
    "Zip": "custom_field:adresse.postal_code",
    "Country": "custom_field:adresse.country",
}


def test_composite_address_mapping_is_valid() -> None:
    targets = validate_mapping_targets(_COMPOSITE, _DECLARED, _DEFS)
    assert {c: (t.reference, t.subpath) for c, t in targets.items()} == {
        "Street": ("adresse", "street"),
        "City": ("adresse", "city"),
        "Zip": ("adresse", "postal_code"),
        "Country": ("adresse", "country"),
    }


def test_subpath_on_non_address_field_rejected() -> None:
    defs = {"note": _addr_def(CustomFieldType.TEXT.value)}
    note = [DeclaredField(family="custom_field", reference="note", required=False)]
    with pytest.raises(ValidationError, match="only valid on an address field"):
        validate_mapping_targets({"X": "custom_field:note.street"}, note, defs)


def test_whole_object_and_subfield_conflict() -> None:
    mapping = {"Whole": "custom_field:adresse", "Street": "custom_field:adresse.street"}
    with pytest.raises(ValidationError, match="both as a whole and by sub-field"):
        validate_mapping_targets(mapping, _DECLARED, _DEFS)


def test_same_subfield_twice_is_duplicate() -> None:
    mapping = {"S1": "custom_field:adresse.street", "S2": "custom_field:adresse.street"}
    with pytest.raises(ValidationError, match="mapped more than once"):
        validate_mapping_targets(mapping, _DECLARED, _DEFS)


def test_distinct_subfields_are_not_duplicates() -> None:
    # The whole point: same field, different sub-paths → NOT a duplicate.
    targets = validate_mapping_targets(
        {"A": "custom_field:adresse.street", "B": "custom_field:adresse.city"}, _DECLARED, _DEFS
    )
    assert len(targets) == 2


# ============================ INTEGRATION (DB + HTTP) ========================


@pytest.fixture
def imports_client(client: AsyncClient, rbac_baseline: None) -> AsyncClient:
    return client


@pytest_asyncio.fixture
async def admin(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    return await make_agent(role=system_roles["admin"])


@pytest_asyncio.fixture
async def template(
    db_session: AsyncSession,
    admin: Agent,
    make_journey_template: MakeJourneyTemplate,
    make_template_step: MakeTemplateStep,
) -> JourneyTemplate:
    """A parcours collecting ONE address custom field (`adresse`)."""
    template = await make_journey_template(agency_id=admin.agency_id)
    await make_template_step(template=template)
    db_session.add(
        CustomFieldDefinition(
            agency_id=admin.agency_id,
            key="adresse",
            label="Adresse",
            field_type=CustomFieldType.ADDRESS.value,
        )
    )
    db_session.add(
        JourneyTemplateField(
            template_id=template.id, kind="custom_field", reference="adresse", position=0
        )
    )
    await db_session.commit()
    return template


_MAPPING = {
    "Email": "email",
    "First": "first_name",
    "Last": "last_name",
    "Street": "custom_field:adresse.street",
    "City": "custom_field:adresse.city",
    "Zip": "custom_field:adresse.postal_code",
    "Country": "custom_field:adresse.country",
}


async def _import(
    client: AsyncClient, admin: Agent, headers: AuthHeaders, template: JourneyTemplate, csv: str
) -> dict:
    body = {"journey_template_id": str(template.id), "mapping": _MAPPING, "csv_text": csv}
    response = await client.post("/imports/cases", json=body, headers=headers(admin))
    assert response.status_code == 200, response.text
    return response.json()


async def _principal_custom_fields(db_session: AsyncSession, case_id: str) -> dict:
    stmt = select(CasePerson).where(CasePerson.case_id == case_id, CasePerson.kind == "principal")
    person = (await db_session.execute(stmt)).scalar_one()
    return dict(person.custom_fields)


async def test_four_columns_assemble_one_address_object(
    imports_client: AsyncClient,
    admin: Agent,
    template: JourneyTemplate,
    agent_headers: AuthHeaders,
    db_session: AsyncSession,
) -> None:
    csv = (
        "Email,First,Last,Street,City,Zip,Country\na@x.io,Alice,A,1 rue de Rivoli,Paris,75001,FR\n"
    )
    report = await _import(imports_client, admin, agent_headers, template, csv)
    assert report["created_count"] == 1
    assert report["created"][0]["field_errors"] == []

    cf = await _principal_custom_fields(db_session, report["created"][0]["case_id"])
    assert cf["adresse"] == {
        "street": "1 rue de Rivoli",
        "city": "Paris",
        "postal_code": "75001",
        "country": "FR",
    }


async def test_partial_address_city_country_only(
    imports_client: AsyncClient,
    admin: Agent,
    template: JourneyTemplate,
    agent_headers: AuthHeaders,
    db_session: AsyncSession,
) -> None:
    # Street + Zip empty → absent from the object (NOT an error, NOT forced-empty).
    csv = "Email,First,Last,Street,City,Zip,Country\nb@x.io,Bob,B,,Lyon,,FR\n"
    report = await _import(imports_client, admin, agent_headers, template, csv)
    assert report["created_count"] == 1
    assert report["created"][0]["field_errors"] == []

    cf = await _principal_custom_fields(db_session, report["created"][0]["case_id"])
    assert cf["adresse"] == {"city": "Lyon", "country": "FR"}


async def test_bad_subfield_is_non_blocking_partial_object_kept(
    imports_client: AsyncClient,
    admin: Agent,
    template: JourneyTemplate,
    agent_headers: AuthHeaders,
    db_session: AsyncSession,
) -> None:
    # Country "FRA" is not ISO-2 → per-column field error, dossier still created
    # with the valid sub-fields assembled (the invalid one dropped).
    csv = "Email,First,Last,Street,City,Zip,Country\nc@x.io,Carol,C,5 Av,Nice,06000,FRA\n"
    report = await _import(imports_client, admin, agent_headers, template, csv)
    assert report["created_count"] == 1
    errors = report["created"][0]["field_errors"]
    assert len(errors) == 1
    assert errors[0]["column"] == "Country"
    assert errors[0]["target"] == "custom_field:adresse.country"
    assert "ISO" in errors[0]["reason"]

    cf = await _principal_custom_fields(db_session, report["created"][0]["case_id"])
    assert cf["adresse"] == {"street": "5 Av", "city": "Nice", "postal_code": "06000"}

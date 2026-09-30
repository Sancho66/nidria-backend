"""Error i18n, wave C1 — the refusals the CLIENT reads on their own space,
and feature 4's lock on every face.

Each refusal keeps its English `detail` (the fallback, byte-identical for
logs) and gains a stable `code` + `params` the front translates. A field is
named by its LABEL in the request language (Accept-Language), never by its
technical key; a blocking prerequisite by its step name resolved in that
same language, passed as a list, never concatenated into the English text.

Families: custom-field format refusals (pure), a client's value on a
requirement, documents (deposit and delete), step validation and
transitions (the lock), comments."""

from typing import Any

import pytest
import pytest_asyncio
from httpx import AsyncClient

from shared.models.agent import Agent
from shared.models.client_case import ClientCase
from shared.models.custom_field import CustomFieldDefinition
from shared.models.expat_user import ExpatUser
from shared.models.rbac import Role
from src.cases.case_fields import COLLECTABLE_CASE_FIELDS
from src.core.config import get_settings
from src.core.exceptions import ValidationError
from src.core.i18n import SUPPORTED_LANGUAGES
from src.custom_fields.custom_fields_validation import validate_and_merge
from src.imports.target_labels import ADDRESS_SUBFIELD_LABELS, CIVIL_LABELS
from src.progress.requirements_eval import COLLECTABLE_BASE_FIELDS
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent
from tests.plugins.case_plugin import MakeClientCase
from tests.plugins.expat_plugin import MakeExpatUser

PDF = {"file": ("passport.pdf", b"%PDF-1.4 fake")}


@pytest.fixture
def ec_client(client: AsyncClient, rbac_baseline: None) -> AsyncClient:
    return client


@pytest_asyncio.fixture
async def admin(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    return await make_agent(role=system_roles["admin"])


@pytest_asyncio.fixture
async def expat(make_expat_user: MakeExpatUser) -> ExpatUser:
    return await make_expat_user(email="client@example.com", first_name="Marie", last_name="Curie")


def _in(headers: dict[str, str], lang: str) -> dict[str, str]:
    return {**headers, "Accept-Language": lang}


def _envelope(response: Any) -> tuple[str, dict[str, Any], str]:
    body = response.json()
    return body["code"], body["params"], body["detail"]


# --- builders --------------------------------------------------------------------------


async def _journey(
    client: AsyncClient, headers: dict[str, str], steps: list[dict[str, object]]
) -> tuple[str, list[str]]:
    tid = (await client.post("/journeys", headers=headers, json={"name": "T"})).json()["id"]
    ids = []
    for spec in steps:
        created = await client.post(f"/journeys/{tid}/steps", headers=headers, json=spec)
        assert created.status_code == 201, created.text
        ids.append(created.json()["id"])
    return tid, ids


async def _requirement(
    client: AsyncClient, headers: dict[str, str], tid: str, sid: str, **body: object
) -> None:
    if body.get("kind") in ("base_field", "custom_field"):
        await client.post(
            f"/journeys/{tid}/fields",
            headers=headers,
            json={"kind": body["kind"], "reference": body["reference"]},
        )
    created = await client.post(
        f"/journeys/{tid}/steps/{sid}/requirements", headers=headers, json=body
    )
    assert created.status_code == 201, created.text


async def _case_with_journey(
    client: AsyncClient,
    headers: dict[str, str],
    make_client_case: MakeClientCase,
    admin: Agent,
    expat: ExpatUser,
    tid: str,
) -> tuple[ClientCase, list[str]]:
    case = await make_client_case(
        agency_id=admin.agency_id, principal_expat_user_id=expat.id, owner_agent_id=admin.id
    )
    steps = (
        await client.post(
            f"/cases/{case.id}/journey", headers=headers, json={"journey_template_id": tid}
        )
    ).json()
    return case, [s["id"] for s in steps]


async def _set_status(
    client: AsyncClient, headers: dict[str, str], case: ClientCase, pid: str, status: str
) -> Any:
    return await client.patch(
        f"/cases/{case.id}/steps/{pid}", headers=headers, json={"status": status}
    )


async def _requirement_id(
    client: AsyncClient, headers: dict[str, str], case: ClientCase, reference: str
) -> tuple[str, str]:
    detail = (await client.get(f"/expat/cases/{case.id}", headers=headers)).json()
    for step in detail["timeline"]:
        for req in step["requirements"]:
            if req["reference"] == reference:
                return req["id"], req["target"]
    raise AssertionError(f"requirement {reference!r} not exposed")


# --- custom-field format refusals (pure) -------------------------------------------------


def _definition(field_type: str, **extra: object) -> CustomFieldDefinition:
    return CustomFieldDefinition(
        key="arrival",
        label="Arrivée",
        label_i18n={"fr": "Arrivée", "ru": "Прибытие"},
        field_type=field_type,
        required=bool(extra.pop("required", False)),
        **extra,
    )


@pytest.mark.parametrize(
    ("field_type", "extra", "value", "code", "params"),
    [
        ("country", {}, "France", "custom_field.country_invalid", {}),
        ("date", {}, "31/31/2026", "custom_field.date_invalid", {}),
        ("number", {}, "douze", "custom_field.number_invalid", {}),
        ("boolean", {}, "peut-être", "custom_field.boolean_invalid", {}),
        ("select", {"options": ["a", "b"]}, "c", "custom_field.option_invalid", {}),
        ("multi_select", {"options": ["a", "b"]}, ["a", "z"], "custom_field.options_invalid", {}),
        ("multi_select", {"options": ["a"]}, "a", "custom_field.options_invalid", {}),
        ("address", {}, "12 rue", "custom_field.address_invalid", {}),
        (
            "address",
            {},
            {"city": "x" * 101},
            "custom_field.address_part_too_long",
            {"max_length": 100},
        ),
    ],
)
def test_custom_field_refusal_is_coded_and_names_the_label_in_the_reader_language(
    field_type: str, extra: dict[str, object], value: object, code: str, params: dict[str, object]
) -> None:
    definition = _definition(field_type, **extra)
    with pytest.raises(ValidationError) as caught:
        validate_and_merge([definition], {}, {"arrival": value}, lang="ru", agency_default="fr")
    assert caught.value.code == code
    assert caught.value.params == {**params, "label": "Прибытие"}
    # The English fallback is unchanged: label + key, for the logs.
    assert caught.value.message.startswith("Field 'Arrivée' (arrival): ")


def test_custom_field_refusal_without_a_language_keeps_the_agency_label() -> None:
    with pytest.raises(ValidationError) as caught:
        validate_and_merge([_definition("number")], {}, {"arrival": "douze"})
    assert caught.value.params == {"label": "Arrivée"}


def test_custom_field_required_and_unknown_keys_are_coded() -> None:
    required = _definition("text", required=True)
    with pytest.raises(ValidationError) as caught:
        validate_and_merge([required], {}, {"arrival": ""}, lang="ru", agency_default="fr")
    assert (caught.value.code, caught.value.params) == (
        "custom_field.value_required",
        {"label": "Прибытие"},
    )
    with pytest.raises(ValidationError) as caught:
        validate_and_merge([required], {}, {"archived_key": "x"})
    # An unknown key has no label to show — and its key is never served.
    assert (caught.value.code, caught.value.params) == ("custom_field.unknown_or_archived", {})


def test_every_collectable_field_has_a_label_in_every_language() -> None:
    """A refused value is named by its label, never by its key: the label
    must exist for every civil column and every case column, x7."""
    for reference in COLLECTABLE_BASE_FIELDS:
        assert set(CIVIL_LABELS[reference]) >= set(SUPPORTED_LANGUAGES), reference
    for column in COLLECTABLE_CASE_FIELDS:
        part = column.split("_", 1)[-1]
        assert set(ADDRESS_SUBFIELD_LABELS[part]) >= set(SUPPORTED_LANGUAGES), column


# --- a client's value on a requirement ------------------------------------------------


async def test_client_value_refusals_name_the_field_in_the_client_language(
    ec_client: AsyncClient,
    admin: Agent,
    expat: ExpatUser,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
    expat_headers: AuthHeaders,
) -> None:
    ah = agent_headers(admin)
    created = await ec_client.post(
        "/agencies/me/custom-fields",
        headers=ah,
        json={
            "key": "arrival_date",
            "label": "Date d'arrivée",
            "label_i18n": {"fr": "Date d'arrivée", "ru": "Дата прибытия"},
            "field_type": "date",
        },
    )
    assert created.status_code == 201, created.text
    tid, (sid,) = await _journey(ec_client, ah, [{"name": "Collecte"}])
    await _requirement(
        ec_client, ah, tid, sid, kind="base_field", reference="date_of_birth", scope="principal"
    )
    await _requirement(
        ec_client, ah, tid, sid, kind="custom_field", reference="arrival_date", scope="principal"
    )
    await _requirement(
        ec_client, ah, tid, sid, kind="document", reference="Passeport", scope="principal"
    )
    await ec_client.post(
        f"/journeys/{tid}/case-fields", headers=ah, json={"case_field": "dest_country"}
    )
    await ec_client.post(
        f"/journeys/{tid}/steps/{sid}/case-requirements",
        headers=ah,
        json={"case_field": "dest_country"},
    )
    case, (pid,) = await _case_with_journey(ec_client, ah, make_client_case, admin, expat, tid)
    assert (await _set_status(ec_client, ah, case, pid, "in_progress")).status_code == 200
    eh = _in(expat_headers(expat), "ru")

    async def put(reference: str, value: object) -> Any:
        rid, target = await _requirement_id(ec_client, eh, case, reference)
        path = "case-requirements" if target == "case" else "requirements"
        return await ec_client.put(
            f"/expat/cases/{case.id}/{path}/{rid}", headers=eh, json={"value": value}
        )

    birth = await put("date_of_birth", "pas une date")
    assert birth.status_code == 422
    code, params, detail = _envelope(birth)
    assert (code, params) == ("requirement.value_invalid", {"label": "Дата рождения"})
    assert detail == "Invalid value for 'date_of_birth'."  # English fallback kept

    arrival = await put("arrival_date", "pas une date")
    assert arrival.status_code == 422
    assert _envelope(arrival)[:2] == ("custom_field.date_invalid", {"label": "Дата прибытия"})

    country = await put("dest_country", "zz")
    assert country.status_code == 422
    assert _envelope(country)[:2] == ("requirement.value_invalid", {"label": "Страна"})

    document = await put("Passeport", "x")
    assert document.status_code == 422
    assert _envelope(document)[:2] == ("requirement.expects_document", {})

    # The step closes: its requirements become read-only.
    assert (await _set_status(ec_client, ah, case, pid, "done")).status_code == 200
    closed = await put("date_of_birth", "1990-01-01")
    assert closed.status_code == 409
    assert _envelope(closed)[:2] == ("requirement.step_not_active", {})


# --- documents ------------------------------------------------------------------------


async def test_document_refusals_are_coded(
    ec_client: AsyncClient,
    admin: Agent,
    expat: ExpatUser,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
    expat_headers: AuthHeaders,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ah, eh = agent_headers(admin), expat_headers(expat)
    tid, (sid,) = await _journey(ec_client, ah, [{"name": "Collecte"}])
    await _requirement(
        ec_client, ah, tid, sid, kind="base_field", reference="phone", scope="principal"
    )
    case, (pid,) = await _case_with_journey(ec_client, ah, make_client_case, admin, expat, tid)
    assert (await _set_status(ec_client, ah, case, pid, "in_progress")).status_code == 200
    base = f"/expat/cases/{case.id}/documents"

    wrong_type = await ec_client.post(base, headers=eh, files={"file": ("notes.txt", b"x")})
    assert wrong_type.status_code == 422
    code, params, _ = _envelope(wrong_type)
    assert code == "document.type_not_allowed"
    assert params == {"accepted": sorted(get_settings().allowed_document_extensions)}

    # A value requirement does not take a file.
    rid, _target = await _requirement_id(ec_client, eh, case, "phone")
    not_doc = await ec_client.post(
        f"/expat/cases/{case.id}/requirements/{rid}/document", headers=eh, files=PDF
    )
    assert not_doc.status_code == 422
    assert _envelope(not_doc)[:2] == ("requirement.not_document", {})

    # Deleting: only your own deposits, never a validated one.
    agency_doc = (await ec_client.post(f"/cases/{case.id}/documents", headers=ah, files=PDF)).json()
    foreign = await ec_client.delete(f"{base}/{agency_doc['id']}", headers=eh)
    assert foreign.status_code == 403
    assert _envelope(foreign)[:2] == ("document.not_own_upload", {})
    own = (await ec_client.post(base, headers=eh, files=PDF)).json()
    await ec_client.patch(
        f"/cases/{case.id}/documents/{own['id']}/validation",
        headers=ah,
        json={"validation_status": "ok"},
    )
    frozen = await ec_client.delete(f"{base}/{own['id']}", headers=eh)
    assert frozen.status_code == 403
    assert _envelope(frozen)[:2] == ("document.validated_locked", {})

    monkeypatch.setattr(get_settings(), "max_document_size_mb", 0)
    too_large = await ec_client.post(base, headers=eh, files=PDF)
    assert too_large.status_code == 413
    assert _envelope(too_large)[:2] == ("document.too_large", {"max_mb": 0})


# --- the lock (feature 4) and the transitions -------------------------------------------

_FILING = {
    "name": "Dépôt du dossier",
    "name_i18n": {"fr": "Dépôt du dossier", "en": "Filing", "ru": "Подача"},
}


async def test_agent_transition_blocked_names_prerequisites_in_the_request_language(
    ec_client: AsyncClient,
    admin: Agent,
    expat: ExpatUser,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    ah = agent_headers(admin)
    tid, (first, second) = await _journey(ec_client, ah, [_FILING, {"name": "Titre de séjour"}])
    await ec_client.put(
        f"/journeys/{tid}/steps/{second}/prerequisites",
        headers=ah,
        json={"prerequisite_step_ids": [first]},
    )
    case, (_filing, residence) = await _case_with_journey(
        ec_client, ah, make_client_case, admin, expat, tid
    )

    blocked = await _set_status(ec_client, _in(ah, "en"), case, residence, "in_progress")
    assert blocked.status_code == 409
    code, params, detail = _envelope(blocked)
    assert (code, params) == ("progress.step_blocked", {"steps": ["Filing"]})
    # The English fallback keeps the scalar name (logs, unmigrated readers).
    assert detail == "Step is blocked by unfinished prerequisite step(s): Dépôt du dossier."

    status_blocked = await _set_status(ec_client, ah, case, residence, "blocked")
    assert status_blocked.status_code == 422
    assert _envelope(status_blocked)[:2] == ("progress.status_not_settable", {})
    todo = await _set_status(ec_client, ah, case, residence, "todo")
    assert todo.status_code == 422
    assert _envelope(todo)[:2] == ("progress.transition_invalid", {})


async def test_client_validation_refusals_name_prerequisites_in_the_client_language(
    ec_client: AsyncClient,
    admin: Agent,
    expat: ExpatUser,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
    expat_headers: AuthHeaders,
) -> None:
    ah = agent_headers(admin)
    tid, (first, second) = await _journey(
        ec_client,
        ah,
        [_FILING, {"name": "Confirmation", "validated_by_type": "expat"}],
    )
    await ec_client.put(
        f"/journeys/{tid}/steps/{second}/prerequisites",
        headers=ah,
        json={"prerequisite_step_ids": [first]},
    )
    case, (filing, confirmation) = await _case_with_journey(
        ec_client, ah, make_client_case, admin, expat, tid
    )
    eh = _in(expat_headers(expat), "ru")

    def validate(pid: str) -> Any:
        return ec_client.post(f"/expat/cases/{case.id}/steps/{pid}/validate", headers=eh)

    # Not active yet: only an active step is validated.
    not_active = await validate(confirmation)
    assert not_active.status_code == 409
    assert _envelope(not_active)[:2] == ("progress.step_not_active", {})
    # Not the client's step to validate.
    assert (await _set_status(ec_client, ah, case, filing, "in_progress")).status_code == 200
    agency_step = await validate(filing)
    assert agency_step.status_code == 409
    assert _envelope(agency_step)[:2] == ("progress.not_validated_by_client", {})

    # The prerequisite is done, the step starts, then the prerequisite is
    # REOPENED: the client's validation hits the lock.
    assert (await _set_status(ec_client, ah, case, filing, "done")).status_code == 200
    assert (await _set_status(ec_client, ah, case, confirmation, "in_progress")).status_code == 200
    assert (await _set_status(ec_client, ah, case, filing, "in_progress")).status_code == 200
    blocked = await validate(confirmation)
    assert blocked.status_code == 409
    assert _envelope(blocked)[:2] == ("progress.step_blocked", {"steps": ["Подача"]})


# --- comments -------------------------------------------------------------------------


async def test_comment_author_border_is_coded(
    ec_client: AsyncClient,
    admin: Agent,
    expat: ExpatUser,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
    expat_headers: AuthHeaders,
) -> None:
    ah, eh = agent_headers(admin), expat_headers(expat)
    tid, _ = await _journey(ec_client, ah, [{"name": "Étape"}])
    case, (pid,) = await _case_with_journey(ec_client, ah, make_client_case, admin, expat, tid)
    agency_comment = (
        await ec_client.post(
            f"/cases/{case.id}/steps/{pid}/comments", headers=ah, json={"body": "Bonjour"}
        )
    ).json()
    edited = await ec_client.patch(
        f"/expat/cases/{case.id}/steps/{pid}/comments/{agency_comment['id']}",
        headers=eh,
        json={"body": "x"},
    )
    assert edited.status_code == 403
    assert _envelope(edited)[:2] == ("comment.not_author", {})

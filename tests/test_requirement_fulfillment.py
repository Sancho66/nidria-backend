"""Step requirements (NEW WAVE 2/4) — CLIENT-SIDE write + auto→DONE +
notifications. The cardinal rule (expat = read-only) is pierced here; the
battery proves the four periphery borders hold, the recompute is
idempotent, and a mail failure never blocks the write.

Covers: exposure of requirements to the client (resolved person name,
archived filtered); value + document fulfillment becomes the source of
truth; bordered authorization (foreign case 404, agent token 401,
inactive/done step read-only, principal fills for family); auto→DONE
respecting the prerequisite lock; agency_validation never self-closes
but arms the owner mail once; notif (a) at activation, (c) at reopen,
gated by the flag; mail best-effort (raise → write still commits)."""

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agency import Agency
from shared.models.agent import Agent
from shared.models.expat_user import ExpatUser
from shared.models.rbac import Role
from src.core import email
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent
from tests.plugins.case_plugin import MakeCasePerson, MakeClientCase
from tests.plugins.expat_plugin import MakeExpatUser


@pytest.fixture
def rf_client(client: AsyncClient, rbac_baseline: None) -> AsyncClient:
    return client


@pytest_asyncio.fixture
async def admin(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    return await make_agent(role=system_roles["admin"])


@pytest_asyncio.fixture
async def expat(make_expat_user: MakeExpatUser) -> ExpatUser:
    return await make_expat_user(email="client@example.com", first_name="Marie", last_name="Curie")


# --- helpers -------------------------------------------------------------------------


async def _step(
    client: AsyncClient, headers: dict[str, str], *, completion_mode: str = "agency_validation"
) -> tuple[str, str]:
    tid = (await client.post("/journeys", headers=headers, json={"name": "T"})).json()["id"]
    step = (
        await client.post(
            f"/journeys/{tid}/steps",
            headers=headers,
            json={"name": "Collecte", "completion_mode": completion_mode},
        )
    ).json()
    return tid, step["id"]


async def _add_req(
    client: AsyncClient, headers: dict[str, str], tid: str, sid: str, **body: object
) -> dict:
    r = await client.post(f"/journeys/{tid}/steps/{sid}/requirements", headers=headers, json=body)
    assert r.status_code == 201, r.text
    return r.json()


async def _assign_start(
    client: AsyncClient, headers: dict[str, str], case_id: str, tid: str, index: int = 0
) -> str:
    steps = (
        await client.post(
            f"/cases/{case_id}/journey", headers=headers, json={"journey_template_id": tid}
        )
    ).json()
    pid = steps[index]["id"]
    started = await client.patch(
        f"/cases/{case_id}/steps/{pid}", headers=headers, json={"status": "in_progress"}
    )
    assert started.status_code == 200, started.text
    return pid


async def _find_req(
    client: AsyncClient, expat_headers: AuthHeaders, expat: ExpatUser, case_id: str, reference: str
) -> dict:
    detail = (await client.get(f"/expat/cases/{case_id}", headers=expat_headers(expat))).json()
    for step in detail["timeline"]:
        for req in step["requirements"]:
            if req["reference"] == reference:
                return req
    raise AssertionError(f"requirement {reference!r} not exposed")


def _mails(subject_fragment: str) -> list[email.OutboxEmail]:
    return [m for m in email.outbox if subject_fragment in m.subject]


# --- exposure ------------------------------------------------------------------------


async def test_client_sees_requirements_with_resolved_person(
    rf_client: AsyncClient,
    admin: Agent,
    expat: ExpatUser,
    make_client_case: MakeClientCase,
    make_case_person: MakeCasePerson,
    agent_headers: AuthHeaders,
    expat_headers: AuthHeaders,
) -> None:
    headers = agent_headers(admin)
    tid, sid = await _step(rf_client, headers)
    await _add_req(
        rf_client,
        headers,
        tid,
        sid,
        kind="base_field",
        reference="passport_number",
        scope="each_person",
    )
    case = await make_client_case(
        agency_id=admin.agency_id, principal_expat_user_id=expat.id, owner_agent_id=admin.id
    )
    await make_case_person(case=case, full_name="Petit Curie")
    await _assign_start(rf_client, headers, str(case.id), tid)

    detail = (await rf_client.get(f"/expat/cases/{case.id}", headers=expat_headers(expat))).json()
    reqs = detail["timeline"][0]["requirements"]
    assert {r["person_label"] for r in reqs} == {"Marie Curie", "Petit Curie"}
    assert all(r["status"] == "pending" for r in reqs)
    assert {r["kind"] for r in reqs} == {"base_field"}


# --- value fulfillment ----------------------------------------------------------------


async def test_value_fulfillment_is_source_of_truth(
    rf_client: AsyncClient,
    admin: Agent,
    expat: ExpatUser,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
    expat_headers: AuthHeaders,
) -> None:
    headers = agent_headers(admin)
    tid, sid = await _step(rf_client, headers)
    await _add_req(
        rf_client,
        headers,
        tid,
        sid,
        kind="base_field",
        reference="passport_number",
        scope="principal",
    )
    case = await make_client_case(
        agency_id=admin.agency_id, principal_expat_user_id=expat.id, owner_agent_id=admin.id
    )
    await _assign_start(rf_client, headers, str(case.id), tid)
    req = await _find_req(rf_client, expat_headers, expat, str(case.id), "passport_number")

    put = await rf_client.put(
        f"/expat/cases/{case.id}/requirements/{req['id']}",
        headers=expat_headers(expat),
        json={"value": "AB12345"},
    )
    assert put.status_code == 200, put.text
    # The returned detail reflects the new state immediately.
    updated = next(r for r in put.json()["timeline"][0]["requirements"] if r["id"] == req["id"])
    assert updated["status"] == "provided"
    # And the AGENT side reads the same value on the person (single source).
    persons = (await rf_client.get(f"/cases/{case.id}", headers=headers)).json()["persons"]
    principal = next(p for p in persons if p["kind"] == "principal")
    assert principal["passport_number"] == "AB12345"


async def test_value_null_clears_back_to_pending(
    rf_client: AsyncClient,
    admin: Agent,
    expat: ExpatUser,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
    expat_headers: AuthHeaders,
) -> None:
    headers = agent_headers(admin)
    tid, sid = await _step(rf_client, headers)
    await _add_req(
        rf_client, headers, tid, sid, kind="base_field", reference="phone", scope="principal"
    )
    case = await make_client_case(
        agency_id=admin.agency_id, principal_expat_user_id=expat.id, owner_agent_id=admin.id
    )
    await _assign_start(rf_client, headers, str(case.id), tid)
    req = await _find_req(rf_client, expat_headers, expat, str(case.id), "phone")
    await rf_client.put(
        f"/expat/cases/{case.id}/requirements/{req['id']}",
        headers=expat_headers(expat),
        json={"value": "+33600000000"},
    )
    cleared = await rf_client.put(
        f"/expat/cases/{case.id}/requirements/{req['id']}",
        headers=expat_headers(expat),
        json={"value": None},
    )
    assert cleared.status_code == 200
    assert cleared.json()["timeline"][0]["requirements"][0]["status"] == "pending"


# --- document fulfillment -------------------------------------------------------------


async def test_document_fulfillment_marks_provided(
    rf_client: AsyncClient,
    admin: Agent,
    expat: ExpatUser,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
    expat_headers: AuthHeaders,
) -> None:
    headers = agent_headers(admin)
    tid, sid = await _step(rf_client, headers)
    await _add_req(
        rf_client, headers, tid, sid, kind="document", reference="Passeport", scope="principal"
    )
    case = await make_client_case(
        agency_id=admin.agency_id, principal_expat_user_id=expat.id, owner_agent_id=admin.id
    )
    await _assign_start(rf_client, headers, str(case.id), tid)
    req = await _find_req(rf_client, expat_headers, expat, str(case.id), "Passeport")

    up = await rf_client.post(
        f"/expat/cases/{case.id}/requirements/{req['id']}/document",
        headers=expat_headers(expat),
        files={"file": ("passport.pdf", b"%PDF-1.4 fake", "application/pdf")},
    )
    assert up.status_code == 200, up.text
    assert up.json()["timeline"][0]["requirements"][0]["status"] == "provided"
    # A document now exists on the case, attached by the expat.
    docs = (await rf_client.get(f"/cases/{case.id}/documents", headers=headers)).json()
    assert any(d["filename"] == "passport.pdf" for d in docs)


async def test_wrong_kind_is_rejected(
    rf_client: AsyncClient,
    admin: Agent,
    expat: ExpatUser,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
    expat_headers: AuthHeaders,
) -> None:
    headers = agent_headers(admin)
    tid, sid = await _step(rf_client, headers)
    await _add_req(
        rf_client, headers, tid, sid, kind="base_field", reference="phone", scope="principal"
    )
    await _add_req(
        rf_client, headers, tid, sid, kind="document", reference="Passeport", scope="principal"
    )
    case = await make_client_case(
        agency_id=admin.agency_id, principal_expat_user_id=expat.id, owner_agent_id=admin.id
    )
    await _assign_start(rf_client, headers, str(case.id), tid)
    field_req = await _find_req(rf_client, expat_headers, expat, str(case.id), "phone")
    doc_req = await _find_req(rf_client, expat_headers, expat, str(case.id), "Passeport")

    # Value endpoint on a document requirement → 422.
    bad_value = await rf_client.put(
        f"/expat/cases/{case.id}/requirements/{doc_req['id']}",
        headers=expat_headers(expat),
        json={"value": "x"},
    )
    assert bad_value.status_code == 422
    # Document endpoint on a field requirement → 422.
    bad_doc = await rf_client.post(
        f"/expat/cases/{case.id}/requirements/{field_req['id']}/document",
        headers=expat_headers(expat),
        files={"file": ("x.pdf", b"data", "application/pdf")},
    )
    assert bad_doc.status_code == 422


# --- periphery authorization ----------------------------------------------------------


async def test_cross_client_cannot_fulfill_another_clients_requirement_404(
    rf_client: AsyncClient,
    admin: Agent,
    expat: ExpatUser,
    make_expat_user: MakeExpatUser,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
    expat_headers: AuthHeaders,
) -> None:
    """THE multi-tenant flaw #1, named and exercised: an authenticated
    expat B (NOT the case principal) targets a REAL requirement of
    client A's case. Must be 404 (not 403, never a write) — the foreign
    case's very existence is not revealed."""
    headers = agent_headers(admin)
    tid, sid = await _step(rf_client, headers)
    await _add_req(
        rf_client, headers, tid, sid, kind="base_field", reference="phone", scope="principal"
    )
    # Case A, owned by `expat` (its principal). The requirement is a real,
    # materialized requirement of A — not an inexistent id.
    case = await make_client_case(
        agency_id=admin.agency_id, principal_expat_user_id=expat.id, owner_agent_id=admin.id
    )
    await _assign_start(rf_client, headers, str(case.id), tid)
    req = await _find_req(rf_client, expat_headers, expat, str(case.id), "phone")

    # B is a DIFFERENT authenticated expat identity, principal of nothing here.
    stranger = await make_expat_user(email="stranger@example.com")
    assert stranger.id != expat.id
    denied = await rf_client.put(
        f"/expat/cases/{case.id}/requirements/{req['id']}",
        headers=expat_headers(stranger),
        json={"value": "INTRUSION"},
    )
    assert denied.status_code == 404  # never reveals the case exists, never writes

    # And nothing was written: A's requirement is still pending.
    after = await _find_req(rf_client, expat_headers, expat, str(case.id), "phone")
    assert after["status"] == "pending"


async def test_client_invalid_custom_value_is_422_not_written(
    rf_client: AsyncClient,
    admin: Agent,
    expat: ExpatUser,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
    expat_headers: AuthHeaders,
) -> None:
    """The DÉGEL-2 validation (type / select options / required) applies
    in FULL on the client write path — a client cannot smuggle an invalid
    value into the DB by going through the portal. Out-of-options select
    and a non-number on a number field each → a readable 422, no write."""
    headers = agent_headers(admin)
    # A select (options A/B) and a number custom field.
    await rf_client.post(
        "/agencies/me/custom-fields",
        headers=headers,
        json={
            "key": "visa_type",
            "label": "Type de visa",
            "field_type": "select",
            "options": ["A", "B"],
        },
    )
    await rf_client.post(
        "/agencies/me/custom-fields",
        headers=headers,
        json={"key": "dossier_no", "label": "N° dossier", "field_type": "number"},
    )
    tid, sid = await _step(rf_client, headers)
    await _add_req(
        rf_client, headers, tid, sid, kind="custom_field", reference="visa_type", scope="principal"
    )
    await _add_req(
        rf_client, headers, tid, sid, kind="custom_field", reference="dossier_no", scope="principal"
    )
    case = await make_client_case(
        agency_id=admin.agency_id, principal_expat_user_id=expat.id, owner_agent_id=admin.id
    )
    await _assign_start(rf_client, headers, str(case.id), tid)
    select_req = await _find_req(rf_client, expat_headers, expat, str(case.id), "visa_type")
    number_req = await _find_req(rf_client, expat_headers, expat, str(case.id), "dossier_no")

    # Value outside the select's options → 422.
    bad_select = await rf_client.put(
        f"/expat/cases/{case.id}/requirements/{select_req['id']}",
        headers=expat_headers(expat),
        json={"value": "Z"},
    )
    assert bad_select.status_code == 422, bad_select.text
    # Free text on a number field → 422.
    bad_number = await rf_client.put(
        f"/expat/cases/{case.id}/requirements/{number_req['id']}",
        headers=expat_headers(expat),
        json={"value": "not-a-number"},
    )
    assert bad_number.status_code == 422, bad_number.text

    # Nothing was written: both stay pending, no invalid value persisted.
    detail = (await rf_client.get(f"/cases/{case.id}", headers=headers)).json()
    principal = next(p for p in detail["persons"] if p["kind"] == "principal")
    assert "visa_type" not in principal["custom_fields"]
    assert "dossier_no" not in principal["custom_fields"]

    # The happy path on the same select still works (proves it's validation,
    # not a blanket block).
    ok = await rf_client.put(
        f"/expat/cases/{case.id}/requirements/{select_req['id']}",
        headers=expat_headers(expat),
        json={"value": "A"},
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["timeline"][0]["requirements"]


async def test_agent_token_rejected(
    rf_client: AsyncClient,
    admin: Agent,
    expat: ExpatUser,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
    expat_headers: AuthHeaders,
) -> None:
    headers = agent_headers(admin)
    tid, sid = await _step(rf_client, headers)
    await _add_req(
        rf_client, headers, tid, sid, kind="base_field", reference="phone", scope="principal"
    )
    case = await make_client_case(
        agency_id=admin.agency_id, principal_expat_user_id=expat.id, owner_agent_id=admin.id
    )
    await _assign_start(rf_client, headers, str(case.id), tid)
    req = await _find_req(rf_client, expat_headers, expat, str(case.id), "phone")
    # An agent token on the expat fulfillment endpoint → 401 (wrong audience).
    denied = await rf_client.put(
        f"/expat/cases/{case.id}/requirements/{req['id']}", headers=headers, json={"value": "x"}
    )
    assert denied.status_code == 401


async def test_done_step_is_read_only(
    rf_client: AsyncClient,
    admin: Agent,
    expat: ExpatUser,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
    expat_headers: AuthHeaders,
) -> None:
    headers = agent_headers(admin)
    tid, sid = await _step(rf_client, headers, completion_mode="agency_validation")
    await _add_req(
        rf_client, headers, tid, sid, kind="base_field", reference="phone", scope="principal"
    )
    case = await make_client_case(
        agency_id=admin.agency_id, principal_expat_user_id=expat.id, owner_agent_id=admin.id
    )
    pid = await _assign_start(rf_client, headers, str(case.id), tid)
    req = await _find_req(rf_client, expat_headers, expat, str(case.id), "phone")
    # Agency closes the step.
    await rf_client.patch(f"/cases/{case.id}/steps/{pid}", headers=headers, json={"status": "done"})
    denied = await rf_client.put(
        f"/expat/cases/{case.id}/requirements/{req['id']}",
        headers=expat_headers(expat),
        json={"value": "x"},
    )
    assert denied.status_code == 409  # not active → read-only


async def test_principal_fills_for_family(
    rf_client: AsyncClient,
    admin: Agent,
    expat: ExpatUser,
    make_client_case: MakeClientCase,
    make_case_person: MakeCasePerson,
    agent_headers: AuthHeaders,
    expat_headers: AuthHeaders,
) -> None:
    headers = agent_headers(admin)
    tid, sid = await _step(rf_client, headers)
    await _add_req(
        rf_client,
        headers,
        tid,
        sid,
        kind="base_field",
        reference="date_of_birth",
        scope="each_person",
    )
    case = await make_client_case(
        agency_id=admin.agency_id, principal_expat_user_id=expat.id, owner_agent_id=admin.id
    )
    family = await make_case_person(case=case, full_name="Petit Curie")
    await _assign_start(rf_client, headers, str(case.id), tid)
    # Find the family member's requirement and fill it AS THE PRINCIPAL.
    detail = (await rf_client.get(f"/expat/cases/{case.id}", headers=expat_headers(expat))).json()
    family_req = next(
        r for r in detail["timeline"][0]["requirements"] if r["person_label"] == "Petit Curie"
    )
    put = await rf_client.put(
        f"/expat/cases/{case.id}/requirements/{family_req['id']}",
        headers=expat_headers(expat),
        json={"value": "2015-04-01"},
    )
    assert put.status_code == 200, put.text
    # The value landed on the FAMILY person, not the principal.
    persons = (await rf_client.get(f"/cases/{case.id}", headers=headers)).json()["persons"]
    fam = next(p for p in persons if p["id"] == str(family.id))
    assert fam["date_of_birth"] == "2015-04-01"


# --- auto→DONE + lock + agency_validation --------------------------------------------


async def test_auto_complete_when_all_provided(
    rf_client: AsyncClient,
    admin: Agent,
    expat: ExpatUser,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
    expat_headers: AuthHeaders,
) -> None:
    headers = agent_headers(admin)
    tid, sid = await _step(rf_client, headers, completion_mode="auto")
    await _add_req(
        rf_client,
        headers,
        tid,
        sid,
        kind="base_field",
        reference="passport_number",
        scope="principal",
    )
    case = await make_client_case(
        agency_id=admin.agency_id, principal_expat_user_id=expat.id, owner_agent_id=admin.id
    )
    pid = await _assign_start(rf_client, headers, str(case.id), tid)
    req = await _find_req(rf_client, expat_headers, expat, str(case.id), "passport_number")
    await rf_client.put(
        f"/expat/cases/{case.id}/requirements/{req['id']}",
        headers=expat_headers(expat),
        json={"value": "AB999"},
    )
    # The step auto-closed (completion_mode=auto, all provided), as SYSTEM.
    step = next(
        s
        for s in (await rf_client.get(f"/cases/{case.id}", headers=headers)).json()["progress"]
        if s["id"] == pid
    )
    assert step["status"] == "done"
    assert step["completed_by_agent_id"] is None  # SYSTEM, not an agent


async def test_auto_complete_blocked_by_prerequisite(
    rf_client: AsyncClient,
    admin: Agent,
    expat: ExpatUser,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
    expat_headers: AuthHeaders,
) -> None:
    """An auto step whose prerequisite is unfinished does NOT self-close
    even when its requirements are all provided — the lock wins. The only
    way to reach this state: start S2 (legally, S1 done), then REOPEN S1
    so its prerequisite is unfinished again while S2 stays active."""
    headers = agent_headers(admin)
    tid = (await rf_client.post("/journeys", headers=headers, json={"name": "T"})).json()["id"]
    s1 = (
        await rf_client.post(f"/journeys/{tid}/steps", headers=headers, json={"name": "S1"})
    ).json()
    s2 = (
        await rf_client.post(
            f"/journeys/{tid}/steps",
            headers=headers,
            json={"name": "S2", "completion_mode": "auto"},
        )
    ).json()
    await rf_client.put(
        f"/journeys/{tid}/steps/{s2['id']}/prerequisites",
        headers=headers,
        json={"prerequisite_step_ids": [s1["id"]]},
    )
    await _add_req(
        rf_client, headers, tid, s2["id"], kind="base_field", reference="phone", scope="principal"
    )
    case = await make_client_case(
        agency_id=admin.agency_id, principal_expat_user_id=expat.id, owner_agent_id=admin.id
    )
    steps = (
        await rf_client.post(
            f"/cases/{case.id}/journey", headers=headers, json={"journey_template_id": tid}
        )
    ).json()
    s1_pid = next(s["id"] for s in steps if s["template_step_id"] == s1["id"])
    s2_pid = next(s["id"] for s in steps if s["template_step_id"] == s2["id"])
    # S1 done → S2 can start (materializes its requirement).
    await rf_client.patch(
        f"/cases/{case.id}/steps/{s1_pid}", headers=headers, json={"status": "done"}
    )
    await rf_client.patch(
        f"/cases/{case.id}/steps/{s2_pid}", headers=headers, json={"status": "in_progress"}
    )
    # Reopen S1 → S2's prerequisite is now unfinished while S2 is active.
    await rf_client.patch(
        f"/cases/{case.id}/steps/{s1_pid}", headers=headers, json={"status": "in_progress"}
    )
    req = await _find_req(rf_client, expat_headers, expat, str(case.id), "phone")
    await rf_client.put(
        f"/expat/cases/{case.id}/requirements/{req['id']}",
        headers=expat_headers(expat),
        json={"value": "+33611111111"},
    )
    s2_now = next(
        s
        for s in (await rf_client.get(f"/cases/{case.id}", headers=headers)).json()["progress"]
        if s["id"] == s2_pid
    )
    assert s2_now["status"] == "in_progress"  # lock held, no auto-close


async def test_agency_validation_arms_owner_mail_once(
    rf_client: AsyncClient,
    admin: Agent,
    expat: ExpatUser,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
    expat_headers: AuthHeaders,
) -> None:
    headers = agent_headers(admin)
    tid, sid = await _step(rf_client, headers, completion_mode="agency_validation")
    await _add_req(
        rf_client,
        headers,
        tid,
        sid,
        kind="base_field",
        reference="passport_number",
        scope="principal",
    )
    case = await make_client_case(
        agency_id=admin.agency_id, principal_expat_user_id=expat.id, owner_agent_id=admin.id
    )
    pid = await _assign_start(rf_client, headers, str(case.id), tid)
    req = await _find_req(rf_client, expat_headers, expat, str(case.id), "passport_number")

    email.outbox.clear()  # drop the activation (a) mail; focus on (b)
    first = await rf_client.put(
        f"/expat/cases/{case.id}/requirements/{req['id']}",
        headers=expat_headers(expat),
        json={"value": "AB123"},
    )
    assert first.json()["timeline"][0]["status"] == "in_progress"  # never self-closes
    ready = _mails("prêt à valider")
    assert len(ready) == 1
    assert ready[0].to == admin.email

    # Idempotence: re-PUT the same (already-met) requirement → no 2nd mail.
    await rf_client.put(
        f"/expat/cases/{case.id}/requirements/{req['id']}",
        headers=expat_headers(expat),
        json={"value": "AB123"},
    )
    assert len(_mails("prêt à valider")) == 1
    assert pid  # silence unused


# --- notifications (a) activation, (c) reopen ----------------------------------------


async def test_activation_notifies_client(
    rf_client: AsyncClient,
    admin: Agent,
    expat: ExpatUser,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    headers = agent_headers(admin)
    tid, sid = await _step(rf_client, headers)
    await _add_req(
        rf_client, headers, tid, sid, kind="base_field", reference="phone", scope="principal"
    )
    case = await make_client_case(
        agency_id=admin.agency_id, principal_expat_user_id=expat.id, owner_agent_id=admin.id
    )
    email.outbox.clear()
    await _assign_start(rf_client, headers, str(case.id), tid)
    sent = _mails("De nouvelles informations")
    assert len(sent) == 1
    assert sent[0].to == expat.email


async def test_reopen_notifies_client_distinct_template(
    rf_client: AsyncClient,
    admin: Agent,
    expat: ExpatUser,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    headers = agent_headers(admin)
    tid, sid = await _step(rf_client, headers, completion_mode="agency_validation")
    await _add_req(
        rf_client, headers, tid, sid, kind="base_field", reference="phone", scope="principal"
    )
    case = await make_client_case(
        agency_id=admin.agency_id, principal_expat_user_id=expat.id, owner_agent_id=admin.id
    )
    pid = await _assign_start(rf_client, headers, str(case.id), tid)
    await rf_client.patch(f"/cases/{case.id}/steps/{pid}", headers=headers, json={"status": "done"})
    email.outbox.clear()
    reopened = await rf_client.patch(
        f"/cases/{case.id}/steps/{pid}", headers=headers, json={"status": "in_progress"}
    )
    assert reopened.status_code == 200
    # Distinct (c) template — the reopening tone, NOT the activation one.
    assert len(_mails("besoin de précisions")) == 1
    assert _mails("De nouvelles informations") == []


async def test_flag_disables_client_notifications(
    rf_client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    expat: ExpatUser,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    headers = agent_headers(admin)
    agency = await db_session.get(Agency, admin.agency_id)
    assert agency is not None
    agency.settings = {**(agency.settings or {}), "step_notifications_enabled": False}
    await db_session.commit()

    tid, sid = await _step(rf_client, headers)
    await _add_req(
        rf_client, headers, tid, sid, kind="base_field", reference="phone", scope="principal"
    )
    case = await make_client_case(
        agency_id=admin.agency_id, principal_expat_user_id=expat.id, owner_agent_id=admin.id
    )
    email.outbox.clear()
    await _assign_start(rf_client, headers, str(case.id), tid)
    assert email.outbox == []  # flag off → no client mail


# --- mail is best-effort -------------------------------------------------------------


async def test_mail_failure_never_blocks_write(
    rf_client: AsyncClient,
    admin: Agent,
    expat: ExpatUser,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
    expat_headers: AuthHeaders,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    headers = agent_headers(admin)
    tid, sid = await _step(rf_client, headers, completion_mode="auto")
    await _add_req(
        rf_client,
        headers,
        tid,
        sid,
        kind="base_field",
        reference="passport_number",
        scope="principal",
    )
    case = await make_client_case(
        agency_id=admin.agency_id, principal_expat_user_id=expat.id, owner_agent_id=admin.id
    )
    pid = await _assign_start(rf_client, headers, str(case.id), tid)
    req = await _find_req(rf_client, expat_headers, expat, str(case.id), "passport_number")

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("SMTP down")

    monkeypatch.setattr("src.progress.progress_manager.send_email", _boom)
    # The write + auto-completion must still succeed despite the mail blowing up.
    put = await rf_client.put(
        f"/expat/cases/{case.id}/requirements/{req['id']}",
        headers=expat_headers(expat),
        json={"value": "AB777"},
    )
    assert put.status_code == 200, put.text
    step = next(
        s
        for s in (await rf_client.get(f"/cases/{case.id}", headers=headers)).json()["progress"]
        if s["id"] == pid
    )
    assert step["status"] == "done"  # auto-completion committed, mail failure swallowed


# --- read parity with the agency face (enriched expat read surface) ------------------

REQUIREMENT_KEYS = {
    "id",
    "kind",
    "reference",
    "scope",
    "status",
    "person_label",
    "value",
    "document_id",
    "target",  # vague C: "person" | "case" (defaults to "person")
}


async def test_expat_value_pending_then_provided(
    rf_client: AsyncClient,
    admin: Agent,
    expat: ExpatUser,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
    expat_headers: AuthHeaders,
) -> None:
    headers = agent_headers(admin)
    tid, sid = await _step(rf_client, headers)
    await _add_req(
        rf_client,
        headers,
        tid,
        sid,
        kind="base_field",
        reference="passport_number",
        scope="principal",
    )
    case = await make_client_case(
        agency_id=admin.agency_id, principal_expat_user_id=expat.id, owner_agent_id=admin.id
    )
    await _assign_start(rf_client, headers, str(case.id), tid)
    req = await _find_req(rf_client, expat_headers, expat, str(case.id), "passport_number")
    assert set(req.keys()) == REQUIREMENT_KEYS
    assert req["value"] is None  # pending → null

    await rf_client.put(
        f"/expat/cases/{case.id}/requirements/{req['id']}",
        headers=expat_headers(expat),
        json={"value": "AB12345"},
    )
    after = await _find_req(rf_client, expat_headers, expat, str(case.id), "passport_number")
    assert after["value"] == "AB12345"  # the real provided value, re-editable


async def test_expat_exposes_active_custom_definitions_only(
    rf_client: AsyncClient,
    admin: Agent,
    expat: ExpatUser,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
    expat_headers: AuthHeaders,
) -> None:
    headers = agent_headers(admin)
    await rf_client.post(
        "/agencies/me/custom-fields",
        headers=headers,
        json={
            "key": "visa_type",
            "label": "Type de visa",
            "field_type": "select",
            "options": ["A", "B"],
        },
    )
    old = await rf_client.post(
        "/agencies/me/custom-fields",
        headers=headers,
        json={"key": "old_field", "label": "Old", "field_type": "text"},
    )
    await rf_client.post(
        f"/agencies/me/custom-fields/{old.json()['id']}/archive", headers=headers
    )  # archived → must not be exposed
    tid, sid = await _step(rf_client, headers)
    await _add_req(
        rf_client, headers, tid, sid, kind="custom_field", reference="visa_type", scope="principal"
    )
    case = await make_client_case(
        agency_id=admin.agency_id, principal_expat_user_id=expat.id, owner_agent_id=admin.id
    )
    await _assign_start(rf_client, headers, str(case.id), tid)

    detail = (await rf_client.get(f"/expat/cases/{case.id}", headers=expat_headers(expat))).json()
    defs = {d["key"]: d for d in detail["custom_field_definitions"]}
    assert "visa_type" in defs
    assert "old_field" not in defs  # archived filtered
    assert defs["visa_type"]["field_type"] == "select"
    assert defs["visa_type"]["options"] == ["A", "B"]
    assert defs["visa_type"]["label"] == "Type de visa"  # human label, not the key


async def test_expat_exposes_document_id(
    rf_client: AsyncClient,
    admin: Agent,
    expat: ExpatUser,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
    expat_headers: AuthHeaders,
) -> None:
    headers = agent_headers(admin)
    tid, sid = await _step(rf_client, headers)
    await _add_req(
        rf_client, headers, tid, sid, kind="document", reference="Passeport", scope="principal"
    )
    case = await make_client_case(
        agency_id=admin.agency_id, principal_expat_user_id=expat.id, owner_agent_id=admin.id
    )
    await _assign_start(rf_client, headers, str(case.id), tid)
    req = await _find_req(rf_client, expat_headers, expat, str(case.id), "Passeport")
    assert req["document_id"] is None  # nothing uploaded yet

    await rf_client.post(
        f"/expat/cases/{case.id}/requirements/{req['id']}/document",
        headers=expat_headers(expat),
        files={"file": ("passport.pdf", b"%PDF-1.4 fake", "application/pdf")},
    )
    after = await _find_req(rf_client, expat_headers, expat, str(case.id), "Passeport")
    assert after["document_id"] is not None
    # document_id joins to the expat documents listing → filename + download.
    docs = (
        await rf_client.get(f"/expat/cases/{case.id}/documents", headers=expat_headers(expat))
    ).json()
    by_id = {d["id"]: d for d in docs}
    assert after["document_id"] in by_id
    assert by_id[after["document_id"]]["filename"] == "passport.pdf"


async def test_expat_exposes_completion_mode_per_step(
    rf_client: AsyncClient,
    admin: Agent,
    expat: ExpatUser,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
    expat_headers: AuthHeaders,
) -> None:
    headers = agent_headers(admin)
    tid, sid = await _step(rf_client, headers, completion_mode="auto")
    await _add_req(
        rf_client, headers, tid, sid, kind="base_field", reference="phone", scope="principal"
    )
    case = await make_client_case(
        agency_id=admin.agency_id, principal_expat_user_id=expat.id, owner_agent_id=admin.id
    )
    await _assign_start(rf_client, headers, str(case.id), tid)
    detail = (await rf_client.get(f"/expat/cases/{case.id}", headers=expat_headers(expat))).json()
    assert detail["timeline"][0]["completion_mode"] == "auto"


async def test_value_and_definitions_consistent_across_faces(
    rf_client: AsyncClient,
    admin: Agent,
    expat: ExpatUser,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
    expat_headers: AuthHeaders,
) -> None:
    """The whole point of factoring: for the SAME requirement, the value
    and the custom definition seen by the client are byte-for-byte what
    the agency sees — one resolution, no drift."""
    headers = agent_headers(admin)
    await rf_client.post(
        "/agencies/me/custom-fields",
        headers=headers,
        json={
            "key": "visa_type",
            "label": "Type de visa",
            "field_type": "select",
            "options": ["A", "B"],
        },
    )
    tid, sid = await _step(rf_client, headers)
    await _add_req(
        rf_client, headers, tid, sid, kind="custom_field", reference="visa_type", scope="principal"
    )
    case = await make_client_case(
        agency_id=admin.agency_id, principal_expat_user_id=expat.id, owner_agent_id=admin.id
    )
    await _assign_start(rf_client, headers, str(case.id), tid)
    req = await _find_req(rf_client, expat_headers, expat, str(case.id), "visa_type")
    await rf_client.put(
        f"/expat/cases/{case.id}/requirements/{req['id']}",
        headers=expat_headers(expat),
        json={"value": "B"},
    )

    # Agency view of the same case.
    agent_detail = (await rf_client.get(f"/cases/{case.id}", headers=headers)).json()
    agent_req = next(
        r for step in agent_detail["progress"] for r in step["requirements"] if r["id"] == req["id"]
    )
    agent_defs = {d["key"]: d for d in agent_detail["custom_field_definitions"]}

    # Client view of the same case.
    expat_detail = (
        await rf_client.get(f"/expat/cases/{case.id}", headers=expat_headers(expat))
    ).json()
    expat_req = next(
        r for step in expat_detail["timeline"] for r in step["requirements"] if r["id"] == req["id"]
    )
    expat_defs = {d["key"]: d for d in expat_detail["custom_field_definitions"]}

    assert expat_req["value"] == agent_req["value"] == "B"  # same resolution
    assert expat_defs["visa_type"] == agent_defs["visa_type"]  # same definition shape

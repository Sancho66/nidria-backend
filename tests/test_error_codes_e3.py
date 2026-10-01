"""Error i18n, wave E3 — the OPERATIONS refusals an AGENT reads (and the
last « not found » a client could still read in English).

Each refusal keeps its English `detail` (the fallback, byte-identical for
logs) and gains a stable dotted `code` + `params` the front translates.
Readable names only, never an id: a provider by its name, a step by its
name RESOLVED in the request language (Accept-Language), else the agency's.

Families: reminders (templates, recipients, state machine, the completed
step), step responsible/validator designation, journey assignment,
external provider assignments, documents / requirements / comments /
attachments « not found », the billed price permission, the document
template size limit, and the import value rules."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from shared.models.client_case import ClientCase
from shared.models.expat_user import ExpatUser
from shared.models.rbac import Role
from src.core.config import get_settings
from src.core.exceptions import ValidationError
from src.core.rbac.permissions import Permission
from src.imports.profile_import_manager import ImportValueMapping, _value_mapping_index
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent
from tests.plugins.case_plugin import MakeClientCase, MakeExternalContact
from tests.plugins.expat_plugin import MakeExpatUser
from tests.plugins.rbac_plugin import MakeRole
from tests.plugins.reminder_plugin import MakeReminder
from tests.plugins.signature_plugin import SOURCE_PDF, FakeProvider

PDF = {"file": ("passport.pdf", b"%PDF-1.4 fake")}
_FUTURE = (datetime.now(UTC) + timedelta(days=3)).isoformat()
_PASSPORT = {
    "name": "Passeport",
    "name_i18n": {"fr": "Passeport", "en": "Passport", "ru": "Паспорт"},
}


@pytest.fixture
def ec_client(client: AsyncClient, rbac_baseline: None) -> AsyncClient:
    return client


@pytest_asyncio.fixture
async def admin(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    return await make_agent(role=system_roles["admin"], first_name="Eloise", last_name="Admin")


@pytest_asyncio.fixture
async def expat(make_expat_user: MakeExpatUser) -> ExpatUser:
    return await make_expat_user(email="client@example.com", first_name="Marie", last_name="Curie")


@pytest_asyncio.fixture
async def provider(make_agent: MakeAgent, admin: Agent, db_session: AsyncSession) -> Agent:
    role = (
        await db_session.execute(
            select(Role).where(Role.is_external.is_(True), Role.name == "external_lawyer")
        )
    ).scalar_one()
    return await make_agent(
        agency_id=admin.agency_id,
        role=role,
        is_external=True,
        email="lawyer@ext.com",
        first_name="Jean",
        last_name="Avocat",
    )


def _in(headers: dict[str, str], lang: str) -> dict[str, str]:
    return {**headers, "Accept-Language": lang}


def _envelope(response: Any) -> tuple[str, dict[str, Any], str]:
    body = response.json()
    return body["code"], body["params"], body["detail"]


async def _case_on_journey(
    client: AsyncClient,
    headers: dict[str, str],
    make_client_case: MakeClientCase,
    admin: Agent,
    expat: ExpatUser,
    steps: list[dict[str, object]],
) -> tuple[ClientCase, str, list[str]]:
    """A case running a fresh journey: (case, template id, progress ids)."""
    tid = (await client.post("/journeys", headers=headers, json={"name": "T"})).json()["id"]
    for spec in steps:
        created = await client.post(f"/journeys/{tid}/steps", headers=headers, json=spec)
        assert created.status_code == 201, created.text
    case = await make_client_case(
        agency_id=admin.agency_id, principal_expat_user_id=expat.id, owner_agent_id=admin.id
    )
    assigned = await client.post(
        f"/cases/{case.id}/journey", headers=headers, json={"journey_template_id": tid}
    )
    assert assigned.status_code == 201, assigned.text
    return case, tid, [step["id"] for step in assigned.json()]


# --- reminders: templates, recipients, creation ------------------------------------------


async def test_reminder_creation_refusals_are_coded(
    ec_client: AsyncClient,
    admin: Agent,
    expat: ExpatUser,
    make_client_case: MakeClientCase,
    make_external_contact: MakeExternalContact,
    agent_headers: AuthHeaders,
) -> None:
    ah = agent_headers(admin)
    case = await make_client_case(agency_id=admin.agency_id, principal_expat_user_id=expat.id)
    other_case = await make_client_case(agency_id=admin.agency_id)
    foreign_contact = await make_external_contact(case=other_case, email="x@y.com")
    no_mail = await make_external_contact(case=case, email=None, name="Maître Dupont")
    base = {
        "channel": "mail",
        "scheduled_at": _FUTURE,
        "recipient_type": "expat",
        "message_body": "Hello",
    }

    async def create(case_id: object, **overrides: object) -> Any:
        body = {**base, **overrides}
        return await ec_client.post(
            f"/cases/{case_id}/reminders",
            headers=ah,
            json={k: v for k, v in body.items() if v is not None},
        )

    unknown_case = await create(uuid.uuid4())
    assert unknown_case.status_code == 404
    assert _envelope(unknown_case) == ("case.not_found", {}, "Case not found.")

    expat_with_contact = await create(case.id, recipient_external_id=str(no_mail.id))
    assert expat_with_contact.status_code == 422
    assert _envelope(expat_with_contact)[:2] == ("reminder.recipient_expat_no_external", {})

    no_contact = await create(case.id, recipient_type="external")
    assert no_contact.status_code == 422
    assert _envelope(no_contact)[:2] == ("reminder.recipient_external_required", {})

    foreign = await create(
        case.id, recipient_type="external", recipient_external_id=str(foreign_contact.id)
    )
    assert foreign.status_code == 422
    assert _envelope(foreign)[:2] == ("case.external_contact_not_found", {})

    no_email = await create(
        case.id, recipient_type="external", recipient_external_id=str(no_mail.id)
    )
    assert no_email.status_code == 422
    code, params, detail = _envelope(no_email)
    assert (code, params) == ("reminder.recipient_no_email", {"name": "Maître Dupont"})
    assert detail == "The external contact has no email address."

    foreign_step = await create(case.id, step_progress_id=str(uuid.uuid4()))
    assert foreign_step.status_code == 422
    assert _envelope(foreign_step)[:2] == ("progress.step_not_found", {})

    unknown_template = await create(
        case.id, message_body=None, message_template_id=str(uuid.uuid4())
    )
    assert unknown_template.status_code == 422
    assert _envelope(unknown_template)[:2] == ("reminder.template_not_found", {})

    empty = await create(case.id, message_body=None)
    assert empty.status_code == 422
    assert _envelope(empty)[:2] == ("reminder.message_required", {})


async def test_reminder_and_template_not_found_are_coded(
    ec_client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    ah = agent_headers(admin)
    missing = uuid.uuid4()
    reminder = await ec_client.get(f"/reminders/{missing}", headers=ah)
    assert reminder.status_code == 404
    assert _envelope(reminder) == ("reminder.not_found", {}, "Reminder not found.")
    for method, path in (
        ("PATCH", f"/message-templates/{missing}"),
        ("DELETE", f"/message-templates/{missing}"),
        ("GET", f"/message-templates/{missing}/translate/estimate"),
    ):
        kwargs: dict[str, Any] = {"json": {"name": "x"}} if method == "PATCH" else {}
        response = await ec_client.request(method, path, headers=ah, **kwargs)
        assert response.status_code == 404, f"{method} {path}"
        assert _envelope(response)[:2] == ("reminder.template_not_found", {}), path


# --- reminders: the state machine and the completed step ------------------------------


async def test_reminder_state_refusals_are_coded(
    ec_client: AsyncClient,
    admin: Agent,
    make_client_case: MakeClientCase,
    make_reminder: MakeReminder,
    agent_headers: AuthHeaders,
) -> None:
    ah = agent_headers(admin)
    case = await make_client_case(agency_id=admin.agency_id)
    sent = await make_reminder(case=case, status="sent")
    approved_mail = await make_reminder(case=case, status="approved")
    pending_whatsapp = await make_reminder(case=case, status="to_approve", channel="whatsapp")

    edited = await ec_client.patch(
        f"/reminders/{sent.id}", headers=ah, json={"message_body": "Autre"}
    )
    assert edited.status_code == 409
    assert _envelope(edited)[:2] == ("reminder.not_editable", {})

    cancelled = await ec_client.post(f"/reminders/{sent.id}/cancel", headers=ah)
    assert cancelled.status_code == 409
    assert _envelope(cancelled)[:2] == ("reminder.not_cancellable", {})

    approved = await ec_client.post(f"/reminders/{approved_mail.id}/approve", headers=ah)
    assert approved.status_code == 409
    assert _envelope(approved)[:2] == ("reminder.not_approvable", {})

    mail_marked = await ec_client.post(f"/reminders/{approved_mail.id}/mark-sent", headers=ah)
    assert mail_marked.status_code == 422
    assert _envelope(mail_marked)[:2] == ("reminder.mark_sent_whatsapp_only", {})

    unapproved = await ec_client.post(f"/reminders/{pending_whatsapp.id}/mark-sent", headers=ah)
    assert unapproved.status_code == 409
    assert _envelope(unapproved)[:2] == ("reminder.mark_sent_not_approved", {})


async def test_step_done_refusal_names_the_step_in_the_request_language(
    ec_client: AsyncClient,
    admin: Agent,
    expat: ExpatUser,
    make_client_case: MakeClientCase,
    make_reminder: MakeReminder,
    agent_headers: AuthHeaders,
) -> None:
    ah = agent_headers(admin)
    case, _tid, (pid,) = await _case_on_journey(
        ec_client, ah, make_client_case, admin, expat, [_PASSPORT]
    )
    for status in ("in_progress", "done"):
        moved = await ec_client.patch(
            f"/cases/{case.id}/steps/{pid}", headers=ah, json={"status": status}
        )
        assert moved.status_code == 200, moved.text
    reminder = await make_reminder(case=case, status="to_approve", step_progress_id=pid)

    refused = await ec_client.post(f"/reminders/{reminder.id}/approve", headers=_in(ah, "en"))
    assert refused.status_code == 409
    code, params, detail = _envelope(refused)
    assert (code, params) == ("reminder.step_done", {"step": "Passport"})
    # The English fallback is the unchanged log sentence (no name glued in).
    assert detail.startswith("This reminder targets a step that is already done")

    russian = await ec_client.post(f"/reminders/{reminder.id}/approve", headers=_in(ah, "ru"))
    assert _envelope(russian)[:2] == ("reminder.step_done", {"step": "Паспорт"})
    # A language the step has no variant for falls back to the agency's.
    hungarian = await ec_client.post(f"/reminders/{reminder.id}/approve", headers=_in(ah, "hu"))
    assert _envelope(hungarian)[:2] == ("reminder.step_done", {"step": "Passeport"})


# --- step responsible / validator designation ----------------------------------------


async def test_responsible_and_validator_refusals_are_coded(
    ec_client: AsyncClient,
    admin: Agent,
    provider: Agent,
    expat: ExpatUser,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    make_client_case: MakeClientCase,
    make_external_contact: MakeExternalContact,
    agent_headers: AuthHeaders,
) -> None:
    ah = agent_headers(admin)
    case, _tid, (pid,) = await _case_on_journey(
        ec_client, ah, make_client_case, admin, expat, [_PASSPORT]
    )
    member = await make_agent(agency_id=admin.agency_id, role=system_roles["member"])
    stranger = await make_agent(role=system_roles["member"])  # another agency
    foreign_contact = await make_external_contact(
        case=await make_client_case(agency_id=admin.agency_id)
    )

    async def responsible(**body: object) -> Any:
        return await ec_client.put(
            f"/cases/{case.id}/steps/{pid}/responsible",
            headers=ah,
            json={k: str(v) if isinstance(v, uuid.UUID) else v for k, v in body.items()},
        )

    async def validator(**body: object) -> Any:
        return await ec_client.put(
            f"/cases/{case.id}/steps/{pid}/validator",
            headers=ah,
            json={k: str(v) if isinstance(v, uuid.UUID) else v for k, v in body.items()},
        )

    cases: list[tuple[Any, int, str, dict[str, Any]]] = [
        (
            await responsible(responsible_type="agent"),
            422,
            "progress.responsible_agent_required",
            {},
        ),
        (
            await responsible(responsible_type="agent", responsible_agent_id=stranger.id),
            422,
            "progress.responsible_not_in_agency",
            {},
        ),
        (
            await responsible(responsible_type="agent", responsible_agent_id=provider.id),
            422,
            "progress.provider_not_assigned",
            {"provider": "Jean Avocat"},
        ),
        (
            await responsible(responsible_type="external"),
            422,
            "progress.responsible_contact_required",
            {},
        ),
        (
            await responsible(
                responsible_type="external", responsible_external_id=foreign_contact.id
            ),
            422,
            "case.external_contact_not_found",
            {},
        ),
        (
            await validator(validated_by_type="agent", validated_by_agent_id=provider.id),
            422,
            "progress.validator_not_internal",
            {},
        ),
        (
            await validator(validated_by_type="external"),
            422,
            "progress.validator_provider_required",
            {},
        ),
        (
            await validator(validated_by_type="external", validated_by_agent_id=member.id),
            422,
            "progress.validator_not_provider",
            {},
        ),
        (
            await validator(validated_by_type="external", validated_by_agent_id=provider.id),
            422,
            "progress.provider_not_assigned",
            {"provider": "Jean Avocat"},
        ),
    ]
    for response, status, code, params in cases:
        assert response.status_code == status, response.text
        assert _envelope(response)[:2] == (code, params), response.text

    missing_step = await ec_client.put(
        f"/cases/{case.id}/steps/{uuid.uuid4()}/responsible",
        headers=ah,
        json={"responsible_type": "expat"},
    )
    assert missing_step.status_code == 404
    assert _envelope(missing_step) == ("progress.step_not_found", {}, "Case step not found.")


async def test_journey_assignment_refusals_are_coded(
    ec_client: AsyncClient,
    admin: Agent,
    expat: ExpatUser,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    ah = agent_headers(admin)
    case, tid, _pids = await _case_on_journey(
        ec_client, ah, make_client_case, admin, expat, [_PASSPORT]
    )
    again = await ec_client.post(
        f"/cases/{case.id}/journey", headers=ah, json={"journey_template_id": tid}
    )
    assert again.status_code == 409
    assert _envelope(again)[:2] == ("case.journey_already_assigned", {})

    fresh = await make_client_case(agency_id=admin.agency_id, principal_expat_user_id=expat.id)
    unknown = await ec_client.post(
        f"/cases/{fresh.id}/journey", headers=ah, json={"journey_template_id": str(uuid.uuid4())}
    )
    assert unknown.status_code == 404
    assert _envelope(unknown)[:2] == ("journey.template_not_found", {})


# --- external provider assignments ----------------------------------------------------


async def test_provider_assignment_refusals_name_provider_and_steps(
    ec_client: AsyncClient,
    admin: Agent,
    provider: Agent,
    expat: ExpatUser,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    ah = agent_headers(admin)
    case, _tid, (pid,) = await _case_on_journey(
        ec_client, ah, make_client_case, admin, expat, [_PASSPORT]
    )
    base = f"/cases/{case.id}/external-assignments"
    member = await make_agent(agency_id=admin.agency_id, role=system_roles["member"])

    internal = await ec_client.post(base, headers=ah, json={"agent_id": str(member.id)})
    assert internal.status_code == 422
    assert _envelope(internal)[:2] == ("external.not_a_provider", {})

    not_assigned = await ec_client.delete(f"{base}/{provider.id}", headers=ah)
    assert not_assigned.status_code == 404
    assert _envelope(not_assigned)[:2] == ("external.assignment_not_found", {})

    assert (
        await ec_client.post(base, headers=ah, json={"agent_id": str(provider.id)})
    ).status_code == 201
    named = await ec_client.put(
        f"/cases/{case.id}/steps/{pid}/responsible",
        headers=ah,
        json={"responsible_type": "agent", "responsible_agent_id": str(provider.id)},
    )
    assert named.status_code == 200, named.text
    still = await ec_client.delete(f"{base}/{provider.id}", headers=_in(ah, "en"))
    assert still.status_code == 409
    code, params, detail = _envelope(still)
    assert (code, params) == (
        "external.still_responsible",
        {"provider": "Jean Avocat", "steps": ["Passport"]},
    )
    assert detail.startswith("This provider is still responsible for at least one step")


# --- documents, requirements, comments, attachments: « not found » --------------------


async def test_agent_document_and_comment_not_found_are_coded(
    ec_client: AsyncClient,
    admin: Agent,
    expat: ExpatUser,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    ah = agent_headers(admin)
    case, _tid, (pid,) = await _case_on_journey(
        ec_client, ah, make_client_case, admin, expat, [_PASSPORT]
    )
    missing = uuid.uuid4()
    expectations: list[tuple[str, str, dict[str, Any], int, str]] = [
        ("GET", f"/cases/{case.id}/documents/{missing}/download", {}, 404, "document.not_found"),
        ("DELETE", f"/cases/{case.id}/documents/{missing}", {}, 404, "document.not_found"),
        (
            "PATCH",
            f"/cases/{case.id}/documents/{missing}/validation",
            {"json": {"validation_status": "ok"}},
            404,
            "document.not_found",
        ),
        (
            "POST",
            f"/cases/{case.id}/requirements/{missing}/document",
            {"files": PDF},
            404,
            "requirement.not_found",
        ),
        ("POST", f"/cases/{missing}/documents", {"files": PDF}, 404, "case.not_found"),
        (
            "POST",
            f"/cases/{case.id}/documents",
            {"files": PDF, "data": {"step_progress_id": str(missing)}},
            422,
            "progress.step_not_found",
        ),
        (
            "POST",
            f"/cases/{case.id}/documents",
            {"files": PDF, "data": {"person_id": str(missing)}},
            404,
            "case.person_not_found",
        ),
        ("GET", f"/cases/{case.id}/steps/{missing}/comments", {}, 404, "progress.step_not_found"),
        (
            "POST",
            f"/cases/{case.id}/steps/{pid}/comments",
            {"json": {"body": "Voir pièce", "document_id": str(missing)}},
            404,
            "document.not_found",
        ),
        (
            "PATCH",
            f"/cases/{case.id}/steps/{pid}/comments/{missing}",
            {"json": {"body": "x"}},
            404,
            "comment.not_found",
        ),
    ]
    for method, path, kwargs, status, code in expectations:
        response = await ec_client.request(method, path, headers=ah, **kwargs)
        assert response.status_code == status, f"{method} {path}: {response.text}"
        assert _envelope(response)[:2] == (code, {}), f"{method} {path}"


async def test_client_not_found_refusals_are_coded(
    ec_client: AsyncClient,
    admin: Agent,
    expat: ExpatUser,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
    expat_headers: AuthHeaders,
) -> None:
    ah, eh = agent_headers(admin), expat_headers(expat)
    case, _tid, (pid,) = await _case_on_journey(
        ec_client, ah, make_client_case, admin, expat, [_PASSPORT]
    )
    missing = uuid.uuid4()
    expectations: list[tuple[str, str, dict[str, Any], str]] = [
        (
            "PUT",
            f"/expat/cases/{case.id}/requirements/{missing}",
            {"json": {"value": "x"}},
            "requirement.not_found",
        ),
        (
            "PUT",
            f"/expat/cases/{case.id}/case-requirements/{missing}",
            {"json": {"value": "x"}},
            "requirement.not_found",
        ),
        (
            "POST",
            f"/expat/cases/{case.id}/requirements/{missing}/document",
            {"files": PDF},
            "requirement.not_found",
        ),
        (
            "GET",
            f"/expat/cases/{case.id}/steps/{pid}/attachments/{missing}/download",
            {},
            "journey.attachment_not_found",
        ),
        ("POST", f"/expat/cases/{case.id}/steps/{missing}/validate", {}, "progress.step_not_found"),
        ("GET", f"/expat/cases/{missing}/documents", {}, "case.not_found"),
        ("GET", f"/expat/cases/{case.id}/documents/{missing}/download", {}, "document.not_found"),
        ("GET", f"/expat/cases/{missing}/steps/{pid}/comments", {}, "case.not_found"),
        (
            "PATCH",
            f"/expat/cases/{case.id}/steps/{pid}/comments/{missing}",
            {"json": {"body": "x"}},
            "comment.not_found",
        ),
    ]
    for method, path, kwargs, code in expectations:
        response = await ec_client.request(method, path, headers=eh, **kwargs)
        assert response.status_code == 404, f"{method} {path}: {response.text}"
        assert _envelope(response)[:2] == (code, {}), f"{method} {path}"


# --- the billed price, the document template size, the import value rules -------------


async def test_billed_price_without_cost_manage_is_coded(
    ec_client: AsyncClient,
    admin: Agent,
    make_agent: MakeAgent,
    make_role: MakeRole,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    case = await make_client_case(agency_id=admin.agency_id)
    role = await make_role(
        permissions=[Permission.CASE_VIEW, Permission.CASE_EDIT], agency_id=admin.agency_id
    )
    editor = await make_agent(agency_id=admin.agency_id, role=role)
    refused = await ec_client.patch(
        f"/cases/{case.id}", headers=agent_headers(editor), json={"billed_amount": "1200"}
    )
    assert refused.status_code == 403
    code, params, detail = _envelope(refused)
    assert (code, params) == ("case.billed_amount_forbidden", {})
    assert detail == "Missing permission: cost.manage."


@pytest.mark.usefixtures("signatures_enabled")
async def test_document_template_over_the_limit_is_coded(
    ec_client: AsyncClient,
    admin: Agent,
    agent_headers: AuthHeaders,
    fake_provider: FakeProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(get_settings(), "max_document_size_mb", 0)
    refused = await ec_client.post(
        "/document-templates",
        headers=agent_headers(admin),
        data={"name": "Mandat"},
        files={"file": ("mandat.pdf", SOURCE_PDF, "application/pdf")},
    )
    assert refused.status_code == 413
    assert _envelope(refused) == (
        "document.too_large",
        {"max_mb": 0},
        "File exceeds the 0 MB limit.",
    )
    assert fake_provider.create_template_calls == []


def test_value_rules_name_unknown_targets_and_the_refused_value() -> None:
    """`import.unknown_targets` ALWAYS carries `targets` (the front's sentence
    interpolates them); a duplicate or blank resolution is another refusal,
    named by the source value it was meant to resolve."""

    def rule(target: str, source: str, value: str = "fr") -> ImportValueMapping:
        return ImportValueMapping(target=target, source_value=source, value=value)

    with pytest.raises(ValidationError) as unknown:
        _value_mapping_index(
            [rule("nationality", "FR"), rule("ghost", "x"), rule("gone", "y")], {"nationality"}
        )
    assert (unknown.value.code, unknown.value.params) == (
        "import.unknown_targets",
        {"targets": ["ghost", "gone"]},
    )
    with pytest.raises(ValidationError) as duplicate:
        _value_mapping_index(
            [rule("nationality", "FR"), rule("nationality", " FR ")], {"nationality"}
        )
    assert (duplicate.value.code, duplicate.value.params) == (
        "import.value_mapping_invalid",
        {"value": "FR"},
    )
    with pytest.raises(ValidationError) as blank:
        _value_mapping_index([rule("nationality", "Française", "   ")], {"nationality"})
    assert (blank.value.code, blank.value.params) == (
        "import.value_mapping_invalid",
        {"value": "Française"},
    )
    assert _value_mapping_index([rule("nationality", " FR ")], {"nationality"}) == {
        ("nationality", "FR"): "fr"
    }

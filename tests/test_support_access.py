"""The platform role and agency switcher cannot read customer content."""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agency import Agency
from shared.models.agent import Agent
from shared.models.client_case import ClientCase
from shared.models.document import Document
from shared.models.rbac import Role
from shared.models.support_access import SupportAccessEvent, SupportAccessGrant
from src.core import storage
from src.core.enums import Audience
from src.core.security import create_access_token
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent
from tests.plugins.case_plugin import MakeCaseNote, MakeClientCase
from tests.plugins.email_plugin import MakeAgencyEmailModule
from tests.plugins.expat_plugin import MakeExpatUser


def test_reviewed_support_routes_are_explicit_and_current() -> None:
    from src.core.rbac.baseline import collect_bindings
    from src.support_access.policy import PLATFORM_OPERATIONS, SUPPORT_METADATA

    declared = {(binding.method, binding.path) for binding in collect_bindings()}
    assert declared >= SUPPORT_METADATA | PLATFORM_OPERATIONS
    assert all(method == "GET" for method, _path in SUPPORT_METADATA)
    assert ("GET", "/jobs/{job_id}/runs") in PLATFORM_OPERATIONS
    assert ("GET", "/cases/{case_id}") not in SUPPORT_METADATA
    assert ("POST", "/document-templates/{template_id}/builder-token") not in SUPPORT_METADATA


@dataclass
class Scene:
    operator: Agent
    admin: Agent
    case: ClientCase
    other_case: ClientCase
    support: dict[str, str]
    document: Document


@pytest_asyncio.fixture
async def scene(
    client: AsyncClient,
    rbac_baseline: None,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
    make_case_note: MakeCaseNote,
    db_session: AsyncSession,
    agent_headers: AuthHeaders,
) -> Scene:
    operator = await make_agent(role=system_roles["superadmin"], first_name="Support")
    admin = await make_agent(role=system_roles["admin"], first_name="Agency")
    person = await make_expat_user(
        first_name="PrivateSentinel", last_name="Customer", email="private-sentinel@example.com"
    )
    case = await make_client_case(agency_id=admin.agency_id, principal_expat_user_id=person.id)
    other_case = await make_client_case(agency_id=admin.agency_id)
    await make_case_note(case=case, body="private note sentinel", is_confidential=True)
    document = Document(
        case_id=case.id,
        filename="private-sentinel.pdf",
        storage_path=f"test/{uuid.uuid4()}",
        uploaded_by_type="agent",
        uploaded_by_id=admin.id,
    )
    db_session.add(document)
    await db_session.commit()
    storage.mock_store[document.storage_path] = b"private PDF sentinel"
    response = await client.post(
        f"/agencies/{admin.agency_id}/enter", headers=agent_headers(operator)
    )
    assert response.status_code == 200, response.text
    return Scene(
        operator,
        admin,
        case,
        other_case,
        {"Authorization": f"Bearer {response.json()['access_token']}"},
        document,
    )


async def _grant(client: AsyncClient, scene: Scene, headers: AuthHeaders) -> str:
    response = await client.post(
        f"/cases/{scene.case.id}/support-access",
        headers=headers(scene.admin),
        json={"operator_id": str(scene.operator.id), "duration_minutes": 15},
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


@pytest.mark.parametrize(
    "route",
    [
        "/cases",
        "/cases/{case_id}",
        "/cases/{case_id}/notes",
        "/cases/{case_id}/documents",
        "/cases/{case_id}/documents/{document_id}/download",
        "/cases/{case_id}/export",
        "/cases/{case_id}/activity",
        "/cases/{case_id}/steps",
        "/agencies/me/export",
        "/client-profiles",
        "/dashboard",
        "/reminders",
        "/email/campaigns",
        "/admin/help/search-misses",
    ],
)
async def test_support_denies_raw_content(client: AsyncClient, scene: Scene, route: str) -> None:
    path = route.format(case_id=scene.case.id, document_id=scene.document.id)
    response = await client.get(path, headers=scene.support)
    assert response.status_code == 403, (path, response.status_code, response.text)
    assert response.json()["code"] == "support.restricted"
    assert "sentinel" not in response.text


@pytest.mark.usefixtures("signatures_enabled")
async def test_operator_keeps_own_workspace_without_foreign_customer_access(
    client: AsyncClient,
    scene: Scene,
    agent_headers: AuthHeaders,
    make_client_case: MakeClientCase,
    email_module_on: None,
    make_agency_email_module: MakeAgencyEmailModule,
) -> None:
    own_case = await make_client_case(agency_id=scene.operator.agency_id)
    await make_agency_email_module(agency_id=scene.operator.agency_id)
    headers = agent_headers(scene.operator)
    me = await client.get("/auth/agent/me", headers=headers)
    assert me.status_code == 200
    assert me.json()["support_mode"] is False
    assert me.json()["can_authorize_support"] is False
    for path in (
        "/cases",
        "/client-profiles",
        "/company-profiles",
        "/dashboard",
        "/reminders",
        "/journeys",
        "/document-templates",
        "/email/campaigns",
        "/email/hot-leads",
        f"/cases/{own_case.id}",
        f"/cases/{own_case.id}/documents",
        f"/cases/{own_case.id}/signature-drafts",
    ):
        response = await client.get(path, headers=headers)
        assert response.status_code == 200, (path, response.status_code, response.text)
        assert "PrivateSentinel" not in response.text
    created = await client.post("/journeys", headers=headers, json={"name": "Own workspace"})
    assert created.status_code == 201, created.text
    for path in (
        f"/cases/{scene.case.id}",
        f"/cases/{scene.case.id}/documents",
        f"/cases/{scene.case.id}/signature-drafts",
        f"/cases/{scene.case.id}/documents/{scene.document.id}/download",
    ):
        response = await client.get(path, headers=headers)
        assert response.status_code == 404, (path, response.status_code, response.text)
        assert "sentinel" not in response.text
    # Home access does not reopen platform-wide search logs or customer masks.
    for method, path in (
        ("GET", "/admin/help/search-misses"),
        ("POST", f"/expat-users/{scene.case.principal_expat_user_id}/impersonate"),
    ):
        response = await client.request(method, path, headers=headers)
        assert response.status_code == 403
        assert response.json()["code"] == "support.restricted"


async def test_legacy_tokens_and_expat_mask_are_closed(client: AsyncClient, scene: Scene) -> None:
    for audience, subject, path in (
        (Audience.AGENT, scene.admin.id, f"/cases/{scene.case.id}"),
        (Audience.EXPAT, scene.case.principal_expat_user_id, "/auth/expat/me"),
        (Audience.EXPAT, scene.case.principal_expat_user_id, "/expat/cases"),
    ):
        token = create_access_token(
            str(subject), audience, extra_claims={"impersonator_id": str(scene.operator.id)}
        )
        response = await client.get(path, headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 403
        assert "sentinel" not in response.text


async def test_metadata_visible_and_original_agency_unchanged(
    client: AsyncClient, scene: Scene, agent_headers: AuthHeaders
) -> None:
    response = await client.get("/support/cases", headers=scene.support)
    assert response.status_code == 200, response.text
    assert response.json()["total"] == 2
    assert set(response.json()["items"][0]) == {"id", "status", "steps", "access_expires_at"}
    assert "sentinel" not in response.text
    for path in ("/agencies/me", "/agencies/me/members", "/journeys", "/message-templates"):
        assert (await client.get(path, headers=scene.support)).status_code == 200, path
    me = await client.get("/auth/agent/me", headers=scene.support)
    assert me.json()["support_mode"] is True
    assert me.json()["can_authorize_support"] is False
    me = await client.get("/auth/agent/me", headers=agent_headers(scene.admin))
    assert me.json()["support_mode"] is False
    assert me.json()["can_authorize_support"] is True
    response = await client.get(f"/cases/{scene.case.id}", headers=agent_headers(scene.admin))
    assert response.status_code == 200
    assert "PrivateSentinel" in response.text


async def test_only_agency_admin_can_authorize_exact_case(
    client: AsyncClient,
    scene: Scene,
    agent_headers: AuthHeaders,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
) -> None:
    member = await make_agent(agency_id=scene.admin.agency_id, role=system_roles["member"])
    foreign_admin = await make_agent(role=system_roles["admin"])
    payload = {"operator_id": str(scene.operator.id), "duration_minutes": 15}
    for headers, expected in (
        (scene.support, 403),
        (agent_headers(scene.operator), 403),
        (agent_headers(member), 403),
        (agent_headers(foreign_admin), 404),
    ):
        response = await client.post(
            f"/cases/{scene.case.id}/support-access", headers=headers, json=payload
        )
        assert response.status_code == expected, response.text
    response = await client.post(
        f"/cases/{scene.case.id}/support-access",
        headers=agent_headers(scene.admin),
        json={**payload, "duration_minutes": 1440},
    )
    assert response.status_code == 422
    response = await client.post(
        f"/cases/{scene.case.id}/support-access",
        headers=agent_headers(scene.admin),
        json={**payload, "operator_id": str(member.id)},
    )
    assert response.status_code == 404


async def test_access_is_scoped_readonly_revocable_and_audited(
    client: AsyncClient,
    scene: Scene,
    agent_headers: AuthHeaders,
    db_session: AsyncSession,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
) -> None:
    content_url = f"/support/cases/{scene.case.id}/content"
    assert (await client.get(content_url, headers=scene.support)).status_code == 403
    grant_id = await _grant(client, scene, agent_headers)
    response = await client.get(content_url, headers=scene.support)
    assert response.status_code == 200, response.text
    assert "PrivateSentinel" in response.text and "private note sentinel" in response.text
    assert "storage_path" not in response.text and "provider_slug" not in response.text
    assert response.headers["cache-control"] == "no-store, private"
    response = await client.get(
        f"/support/cases/{scene.case.id}/documents/{scene.document.id}/download",
        headers=scene.support,
    )
    assert response.status_code == 200 and response.content == b"private PDF sentinel"
    assert (
        await client.get(f"/support/cases/{scene.other_case.id}/content", headers=scene.support)
    ).status_code == 403
    # Raw URLs stay closed even with a grant; they can carry edit/signing capabilities.
    assert (await client.get(f"/cases/{scene.case.id}", headers=scene.support)).status_code == 403
    assert (
        await client.patch(
            f"/cases/{scene.case.id}", headers=scene.support, json={"status": "closed"}
        )
    ).status_code == 403
    other_operator = await make_agent(role=system_roles["superadmin"])
    token = create_access_token(
        str(scene.admin.id),
        Audience.AGENT,
        extra_claims={"impersonator_id": str(other_operator.id)},
    )
    assert (
        await client.get(content_url, headers={"Authorization": f"Bearer {token}"})
    ).status_code == 403
    journal_url = f"/cases/{scene.case.id}/support-access"
    journal = await client.get(journal_url, headers=agent_headers(scene.admin))
    assert journal.status_code == 200
    assert {e["action"] for e in journal.json()["events"]} == {
        "granted",
        "case_read",
        "document_read",
    }
    assert "sentinel" not in journal.text
    assert (await client.get(journal_url, headers=scene.support)).status_code == 403
    response = await client.delete(f"{journal_url}/{grant_id}", headers=agent_headers(scene.admin))
    assert response.status_code == 204
    assert (await client.get(content_url, headers=scene.support)).status_code == 403
    assert (
        await client.get(
            f"/support/cases/{scene.case.id}/documents/{scene.document.id}/download",
            headers=scene.support,
        )
    ).status_code == 403
    events = list(
        await db_session.scalars(
            select(SupportAccessEvent).where(SupportAccessEvent.case_id == scene.case.id)
        )
    )
    assert len(events) == 4
    assert {e.actor_id for e in events if e.action.endswith("read")} == {scene.operator.id}


async def test_expiration_blocks_same_token_and_storage(
    client: AsyncClient, scene: Scene, agent_headers: AuthHeaders, db_session: AsyncSession
) -> None:
    grant_id = await _grant(client, scene, agent_headers)
    grant = await db_session.get(SupportAccessGrant, uuid.UUID(grant_id))
    assert grant
    grant.created_at = datetime.now(UTC) - timedelta(minutes=20)
    grant.expires_at = datetime.now(UTC) - timedelta(minutes=5)
    await db_session.commit()
    assert (
        await client.get(f"/support/cases/{scene.case.id}/content", headers=scene.support)
    ).status_code == 403
    assert (
        await client.get(
            f"/support/cases/{scene.case.id}/documents/{scene.document.id}/download",
            headers=scene.support,
        )
    ).status_code == 403
    result = await client.get("/support/cases", headers=scene.support)
    assert all(row["access_expires_at"] is None for row in result.json()["items"])


async def test_demoted_operator_cannot_reuse_current_or_legacy_mask(
    client: AsyncClient, scene: Scene, db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    legacy = create_access_token(
        str(scene.admin.id),
        Audience.AGENT,
        extra_claims={"impersonator_id": str(scene.operator.id)},
    )
    scene.operator.role = system_roles["admin"]
    scene.operator.role_id = system_roles["admin"].id
    await db_session.commit()
    for headers in (scene.support, {"Authorization": f"Bearer {legacy}"}):
        response = await client.get(f"/cases/{scene.case.id}", headers=headers)
        assert response.status_code == 401
        assert "sentinel" not in response.text


async def test_revocation_survives_billing_hold_and_pending_legal_consent(
    client: AsyncClient, scene: Scene, agent_headers: AuthHeaders, db_session: AsyncSession
) -> None:
    from src.consents.consents_seed import seed_consent_documents

    grant_id = await _grant(client, scene, agent_headers)
    await seed_consent_documents(db_session)
    await db_session.execute(
        update(Agency)
        .where(Agency.id == scene.admin.agency_id)
        .values(trial_ends_at=datetime.now(UTC) - timedelta(days=1))
    )
    await db_session.commit()
    headers = agent_headers(scene.admin)
    assert (await client.get("/cases", headers=headers)).json()["code"] == "consent.required"
    journal = f"/cases/{scene.case.id}/support-access"
    assert (await client.get(journal, headers=headers)).status_code == 200
    assert (await client.delete(f"{journal}/{grant_id}", headers=headers)).status_code == 204
    assert (
        await client.get(f"/support/cases/{scene.case.id}/content", headers=scene.support)
    ).status_code == 403


async def test_failed_audit_write_never_loads_private_content(
    client: AsyncClient, scene: Scene, agent_headers: AuthHeaders, db_session: AsyncSession
) -> None:
    from src.support_access.manager import SupportAccessManager

    await _grant(client, scene, agent_headers)
    manager = SupportAccessManager(db_session)
    with (
        patch.object(
            db_session, "commit", AsyncMock(side_effect=RuntimeError("Audit unavailable"))
        ),
        patch.object(manager.repo, "people", AsyncMock()) as people,
    ):
        with pytest.raises(RuntimeError, match="Audit unavailable"):
            await manager.content(scene.admin, scene.operator.id, scene.case.id)
        people.assert_not_awaited()
    await db_session.rollback()

"""One-off PDFs never escape their case and use the normal signing rails."""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session, sessionmaker

from shared.models.agent import Agent
from shared.models.case_step_progress import CaseStepProgress
from shared.models.case_step_requirement import CaseStepRequirement
from shared.models.client_case import ClientCase
from shared.models.document import Document
from shared.models.document_template import DocumentTemplate
from shared.models.expat_user import ExpatUser
from shared.models.rbac import Role
from src.core.config import get_settings
from src.signatures import ledger
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent
from tests.plugins.case_plugin import MakeClientCase
from tests.plugins.expat_plugin import MakeExpatUser
from tests.plugins.signature_plugin import SOURCE_PDF, FakeProvider

pytestmark = pytest.mark.usefixtures("rbac_baseline", "signatures_enabled")


@dataclass
class Scene:
    admin: Agent
    headers: dict[str, str]
    case_id: uuid.UUID
    progress_id: str
    person_id: str
    expat: ExpatUser
    template_id: str
    step_id: str


@pytest_asyncio.fixture
async def scene(
    client: AsyncClient,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
    fake_provider: FakeProvider,
) -> Scene:
    admin = await make_agent(role=system_roles["admin"])
    headers = agent_headers(admin)
    fake_provider.default_roles = ["Signataire 1"]
    expat = await make_expat_user(activated=True)
    case = await make_client_case(agency_id=admin.agency_id, principal_expat_user_id=expat.id)
    template = (await client.post("/journeys", headers=headers, json={"name": "Onboarding"})).json()
    step = (
        await client.post(
            f"/journeys/{template['id']}/steps", headers=headers, json={"name": "Agreement"}
        )
    ).json()
    response = await client.post(
        f"/cases/{case.id}/journey", headers=headers, json={"journey_template_id": template["id"]}
    )
    assert response.status_code == 201, response.text
    progress_id = response.json()[0]["id"]
    detail = (await client.get(f"/cases/{case.id}", headers=headers)).json()
    return Scene(
        admin,
        headers,
        case.id,
        progress_id,
        detail["principal_person_id"],
        expat,
        template["id"],
        step["id"],
    )


async def draft(
    client: AsyncClient, scene: Scene, *, sync: bool = True, name: str = "Individual agreement"
) -> dict:
    response = await client.post(
        f"/cases/{scene.case_id}/signature-drafts",
        headers=scene.headers,
        data={"name": name, "progress_id": scene.progress_id, "person_id": scene.person_id},
        files={"file": ("private.pdf", SOURCE_PDF, "application/pdf")},
    )
    assert response.status_code == 201, response.text
    row = response.json()
    if sync:
        response = await client.post(
            f"/cases/{scene.case_id}/signature-drafts/{row['id']}/builder-sync",
            headers=scene.headers,
        )
        assert response.status_code == 200, response.text
        row = response.json()
    return row


async def activate(client: AsyncClient, scene: Scene) -> None:
    response = await client.patch(
        f"/cases/{scene.case_id}/steps/{scene.progress_id}",
        headers=scene.headers,
        json={"status": "in_progress"},
    )
    assert response.status_code == 200, response.text


async def test_one_off_full_cycle_and_no_reusable_template(
    client: AsyncClient,
    scene: Scene,
    fake_provider: FakeProvider,
    give_credits,
    db_session: AsyncSession,
    expat_headers: AuthHeaders,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await give_credits(scene.admin.agency_id, 2)
    await activate(client, scene)
    row = await draft(client, scene)
    assert row["status"] == "draft"
    assert fake_provider.create_calls == []
    assert (await client.get("/document-templates", headers=scene.headers)).json() == []
    assert (
        await client.get(f"/document-templates/{row['id']}", headers=scene.headers)
    ).status_code == 404
    prefix = f"/cases/{scene.case_id}/signature-drafts/{row['id']}"
    sent = await client.post(prefix + "/submit", headers=scene.headers)
    assert sent.status_code == 200, sent.text
    assert sent.json()["status"] == "sent"
    repeated = await client.post(prefix + "/submit", headers=scene.headers)
    assert repeated.json()["request_id"] == sent.json()["request_id"]
    assert len(fake_provider.create_calls) == 1
    assert await ledger.balance(db_session, scene.admin.agency_id) == (1, 1)
    for endpoint in ("builder-token", "builder-sync"):
        assert (
            await client.post(prefix + "/" + endpoint, headers=scene.headers)
        ).status_code == 409
    assert (await client.delete(prefix, headers=scene.headers)).status_code == 409
    client_tasks = await client.get(
        f"/expat/cases/{scene.case_id}/signatures", headers=expat_headers(scene.expat)
    )
    assert client_tasks.status_code == 200, client_tasks.text
    assert [task["reference"] for task in client_tasks.json()] == ["Individual agreement"]
    assert client_tasks.json()[0]["embed_slug"]
    signer_id = client_tasks.json()[0]["signer_id"]
    monkeypatch.setenv("DOCUSEAL_WEBHOOK_SECRET", "test-oneoff-webhook")
    get_settings.cache_clear()
    from src.signatures.signatures_router import SECRET_HEADER

    completed = await client.post(
        "/webhooks/docuseal",
        headers={SECRET_HEADER: "test-oneoff-webhook"},
        json={"event_type": "form.completed", "data": {"external_id": signer_id}},
    )
    assert completed.status_code == 200, completed.text
    assert completed.json()["status"] == "processed"
    assert await ledger.balance(db_session, scene.admin.agency_id) == (1, 0)
    requirement = await db_session.scalar(
        select(CaseStepRequirement).where(
            CaseStepRequirement.document_template_id == uuid.UUID(row["id"])
        )
    )
    assert requirement is not None and requirement.status == "provided"
    assert requirement.document_id is not None
    assert (
        len(
            list(
                await db_session.scalars(select(Document).where(Document.case_id == scene.case_id))
            )
        )
        == 2
    )


async def test_future_step_queues_then_sends_on_activation(
    client: AsyncClient,
    scene: Scene,
    fake_provider: FakeProvider,
    give_credits,
    expat_headers: AuthHeaders,
) -> None:
    await give_credits(scene.admin.agency_id, 1)
    row = await draft(client, scene)
    result = await client.post(
        f"/cases/{scene.case_id}/signature-drafts/{row['id']}/submit", headers=scene.headers
    )
    assert result.status_code == 200, result.text
    assert result.json()["status"] == "queued"
    assert fake_provider.create_calls == []
    assert (
        await client.get(
            f"/expat/cases/{scene.case_id}/signatures", headers=expat_headers(scene.expat)
        )
    ).json() == []
    await activate(client, scene)
    assert len(fake_provider.create_calls) == 1
    assert (
        len(
            (
                await client.get(
                    f"/expat/cases/{scene.case_id}/signatures", headers=expat_headers(scene.expat)
                )
            ).json()
        )
        == 1
    )


async def test_insufficient_credit_is_atomic_and_retryable(
    client: AsyncClient,
    scene: Scene,
    db_session: AsyncSession,
    fake_provider: FakeProvider,
) -> None:
    await activate(client, scene)
    row = await draft(client, scene)
    response = await client.post(
        f"/cases/{scene.case_id}/signature-drafts/{row['id']}/submit", headers=scene.headers
    )
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "signatures.credits_insufficient"
    await db_session.rollback()
    template = await db_session.get(DocumentTemplate, uuid.UUID(row["id"]))
    assert template is not None and template.submitted_at is None
    assert (
        list(
            await db_session.scalars(
                select(CaseStepRequirement).where(
                    CaseStepRequirement.document_template_id == uuid.UUID(row["id"])
                )
            )
        )
        == []
    )
    assert fake_provider.create_calls == []


async def test_fields_duplicate_names_and_reuse_are_rejected(
    client: AsyncClient,
    scene: Scene,
    fake_provider: FakeProvider,
) -> None:
    row = await draft(client, scene, sync=False)
    prefix = f"/cases/{scene.case_id}/signature-drafts/{row['id']}"
    response = await client.post(prefix + "/submit", headers=scene.headers)
    assert response.status_code == 422
    assert response.json()["code"] == "signatures.one_off_fields_required"
    fake_provider.default_roles = ["Signataire 1", "Signataire 2"]
    await client.post(prefix + "/builder-sync", headers=scene.headers)
    assert (await client.post(prefix + "/submit", headers=scene.headers)).status_code == 422
    fake_provider.default_roles = ["Signataire 1"]
    await client.post(prefix + "/builder-sync", headers=scene.headers)
    assert (await client.post(prefix + "/submit", headers=scene.headers)).status_code == 200
    response = await client.post(
        f"/cases/{scene.case_id}/signature-drafts",
        headers=scene.headers,
        data={"name": row["name"], "person_id": scene.person_id, "progress_id": scene.progress_id},
        files={"file": ("p.pdf", SOURCE_PDF, "application/pdf")},
    )
    assert response.status_code == 409
    # A later journey edit cannot silently group a reusable template with the private PDF.
    response = await client.post(
        f"/journeys/{scene.template_id}/steps/{scene.step_id}/requirements",
        headers=scene.headers,
        json={"kind": "document", "reference": row["name"], "scope": "principal"},
    )
    assert response.status_code == 409
    response = await client.post(
        f"/journeys/{scene.template_id}/steps/{scene.step_id}/requirements",
        headers=scene.headers,
        json={
            "kind": "document",
            "reference": "Another name",
            "scope": "principal",
            "signature_required": True,
            "document_template_id": row["id"],
        },
    )
    assert response.status_code == 404


async def test_cross_case_tenant_reader_and_support_are_denied(
    client: AsyncClient,
    scene: Scene,
    agent_headers: AuthHeaders,
    make_agent: MakeAgent,
    make_client_case: MakeClientCase,
    system_roles: dict[str, Role],
) -> None:
    row = await draft(client, scene)
    other_case = await make_client_case(agency_id=scene.admin.agency_id)
    outsiders = [
        await make_agent(role=system_roles["admin"]),
        await make_agent(agency_id=scene.admin.agency_id, role=system_roles["viewer"]),
    ]
    prefix = f"/cases/{scene.case_id}/signature-drafts/{row['id']}"
    for actor, expected in zip(outsiders, [404, 403], strict=True):
        for endpoint in ("submit", "builder-token", "builder-sync"):
            assert (
                await client.post(prefix + "/" + endpoint, headers=agent_headers(actor))
            ).status_code == expected
        assert (await client.delete(prefix, headers=agent_headers(actor))).status_code == expected
    for endpoint in ("submit", "builder-token", "builder-sync"):
        assert (
            await client.post(
                f"/cases/{other_case.id}/signature-drafts/{row['id']}/{endpoint}",
                headers=scene.headers,
            )
        ).status_code == 404
    operator = await make_agent(role=system_roles["superadmin"])
    switched = await client.post(
        f"/agencies/{scene.admin.agency_id}/enter", headers=agent_headers(operator)
    )
    support = {"Authorization": f"Bearer {switched.json()['access_token']}"}
    assert (
        await client.get(f"/cases/{scene.case_id}/signature-drafts", headers=support)
    ).status_code == 403
    assert (await client.post(prefix + "/builder-token", headers=support)).status_code == 403


async def test_draft_deletion_and_completed_step_guard(
    client: AsyncClient, scene: Scene, fake_provider: FakeProvider
) -> None:
    row = await draft(client, scene)
    assert (
        await client.delete(
            f"/cases/{scene.case_id}/signature-drafts/{row['id']}", headers=scene.headers
        )
    ).status_code == 204
    assert len(fake_provider.archive_calls) == 1
    await activate(client, scene)
    result = await client.patch(
        f"/cases/{scene.case_id}/steps/{scene.progress_id}",
        headers=scene.headers,
        json={"status": "done"},
    )
    assert result.status_code == 200, result.text
    response = await client.post(
        f"/cases/{scene.case_id}/signature-drafts",
        headers=scene.headers,
        data={"name": "Later", "person_id": scene.person_id, "progress_id": scene.progress_id},
        files={"file": ("p.pdf", SOURCE_PDF, "application/pdf")},
    )
    assert response.status_code == 409 and response.json()["code"] == "signatures.step_completed"


async def test_one_off_keeps_standard_reminders_pending(
    client: AsyncClient,
    scene: Scene,
    give_credits,
    db_session: AsyncSession,
    sync_session_local: sessionmaker[Session],
) -> None:
    from src.reminders.reminders_jobs import create_auto_reminders

    await give_credits(scene.admin.agency_id, 1)
    await activate(client, scene)
    row = await draft(client, scene)
    response = await client.post(
        f"/cases/{scene.case_id}/signature-drafts/{row['id']}/submit", headers=scene.headers
    )
    assert response.status_code == 200, response.text
    await db_session.execute(
        update(CaseStepProgress)
        .where(CaseStepProgress.id == uuid.UUID(scene.progress_id))
        .values(updated_at=datetime.now(UTC) - timedelta(days=25))
    )
    await db_session.commit()
    with sync_session_local() as db:
        result = create_auto_reminders(db, log=lambda _: None)
    assert result["created"] == 1


async def test_coinciding_labels_cannot_send_another_pdf(
    scene: Scene,
    db_session: AsyncSession,
    fake_provider: FakeProvider,
) -> None:
    from src.core.exceptions import ConflictError
    from src.signatures.signatures_manager import SignaturesManager

    case = await db_session.get(ClientCase, scene.case_id)
    progress = await db_session.get(CaseStepProgress, uuid.UUID(scene.progress_id))
    assert case is not None and progress is not None
    rows = [CaseStepRequirement(document_template_id=uuid.uuid4()) for _ in range(2)]
    with pytest.raises(ConflictError) as error:
        await SignaturesManager(db_session)._send_one(case, progress, "Same label", rows)
    assert error.value.code == "signatures.document_name_exists"
    assert fake_provider.create_calls == []

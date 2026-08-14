"""Balayage des brouillons de modèles de document (lot 14/08).

Le pendant du brouillon invisible : l'état `draft` fait que l'agence ne voit
pas un modèle qu'elle n'a jamais commencé ; ce job fait qu'il ne reste pas.
Sans lui, « invisible » voudrait dire « accumulé en silence » — le fantôme
serait toujours là, juste caché, et chaque abandon coûterait encore un
template chez le provider et un PDF dans le storage.

Le témoin couvre les quatre décisions du balayage : ce qu'il emporte, ce
qu'il épargne parce que c'est trop jeune, ce qu'il épargne parce que c'est
promu, et ce qu'il refuse de toucher parce que c'est référencé.
"""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from shared.models.agent import Agent
from shared.models.document_template import DocumentTemplate
from shared.models.rbac import Role
from shared.models.step_requirement import StepRequirement
from src.core import storage
from src.document_templates import document_templates_jobs
from src.document_templates.document_templates_jobs import (
    DRAFT_TTL_HOURS,
    sweep_document_template_drafts,
)
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent
from tests.plugins.journey_plugin import MakeJourneyTemplate, MakeTemplateStep
from tests.plugins.signature_plugin import SOURCE_PDF, FakeProvider
from tests.test_signatures import _document_template

pytestmark = pytest.mark.usefixtures("rbac_baseline", "signatures_enabled")


@pytest_asyncio.fixture
async def admin(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    return await make_agent(role=system_roles["admin"])


async def _draft(client: AsyncClient, headers: dict[str, str], name: str = "Abandonné") -> dict:
    """Un modèle créé et JAMAIS commencé : l'agence a fermé le builder."""
    r = await client.post(
        "/document-templates",
        headers=headers,
        data={"name": name},
        files={"file": ("mandat.pdf", SOURCE_PDF, "application/pdf")},
    )
    assert r.status_code == 201, r.text
    assert r.json()["state"] == "draft"
    return r.json()


def _sweep(sync_session_local: sessionmaker[Session], **kwargs: object) -> dict:
    with sync_session_local() as sync_db:
        return sweep_document_template_drafts(sync_db, log=lambda _line: None, **kwargs)  # type: ignore[arg-type]


async def _age(db_session, template_id: str, hours: float) -> None:
    """Vieillir un modèle : le TTL se compte depuis `created_at`."""
    row = (
        await db_session.execute(
            select(DocumentTemplate).where(DocumentTemplate.id == uuid.UUID(template_id))
        )
    ).scalar_one()
    row.created_at = datetime.now(UTC) - timedelta(hours=hours)
    await db_session.commit()


async def test_sweep_deletes_an_abandoned_draft_with_its_three_supports(
    client: AsyncClient,
    admin: Agent,
    agent_headers: AuthHeaders,
    fake_provider: FakeProvider,
    db_session,
    sync_session_local: sessionmaker[Session],
) -> None:
    """L'abandon coûte trois choses : une ligne, un PDF, un template chez le
    provider. Le balayage les emporte toutes les trois, comme la suppression
    manuelle — sinon « invisible » ne serait qu'un tapis sous lequel on
    pousse la poussière."""
    headers = agent_headers(admin)
    draft = await _draft(client, headers)
    ref = fake_provider.create_template_calls[-1]["ref"]
    path = (
        await db_session.execute(
            select(DocumentTemplate.storage_path).where(
                DocumentTemplate.id == uuid.UUID(draft["id"])
            )
        )
    ).scalar_one()
    assert path in storage.mock_store
    await _age(db_session, draft["id"], DRAFT_TTL_HOURS + 1)

    stats = await asyncio.to_thread(_sweep, sync_session_local)
    assert stats == {
        "swept": 1,
        "provider_archived": 1,
        "storage_purged": 1,
        "referenced_skipped": 0,
    }
    db_session.expire_all()  # le job a écrit par SA propre session sync
    assert (await db_session.execute(select(DocumentTemplate))).scalars().all() == []
    assert ref in fake_provider.archive_calls
    assert path not in storage.mock_store


async def test_sweep_spares_a_draft_younger_than_the_ttl(
    client: AsyncClient,
    admin: Agent,
    agent_headers: AuthHeaders,
    fake_provider: FakeProvider,
    db_session,
    sync_session_local: sessionmaker[Session],
) -> None:
    """Une séance de travail interrompue n'est pas un abandon : le déjeuner,
    la réunion, la reprise le lendemain matin. Le TTL doit les couvrir."""
    headers = agent_headers(admin)
    draft = await _draft(client, headers)
    await _age(db_session, draft["id"], DRAFT_TTL_HOURS - 1)

    stats = await asyncio.to_thread(_sweep, sync_session_local)
    assert stats["swept"] == 0
    db_session.expire_all()
    assert (await db_session.execute(select(DocumentTemplate))).scalars().one() is not None


async def test_sweep_never_touches_a_promoted_template_however_old(
    client: AsyncClient,
    admin: Agent,
    agent_headers: AuthHeaders,
    fake_provider: FakeProvider,
    db_session,
    sync_session_local: sessionmaker[Session],
) -> None:
    """Un modèle promu est DANS la bibliothèque : son âge ne le condamne
    jamais. Un mandat posé une fois sert des années."""
    headers = agent_headers(admin)
    template = await _document_template(client, headers, synced=True)
    assert template["state"] == "active"
    await _age(db_session, template["id"], DRAFT_TTL_HOURS * 100)

    stats = await asyncio.to_thread(_sweep, sync_session_local)
    assert stats["swept"] == 0
    db_session.expire_all()
    listed = (await client.get("/document-templates", headers=headers)).json()
    assert [t["id"] for t in listed] == [template["id"]]


async def test_sweep_spares_and_names_a_referenced_draft(
    client: AsyncClient,
    admin: Agent,
    agent_headers: AuthHeaders,
    fake_provider: FakeProvider,
    db_session,
    sync_session_local: sessionmaker[Session],
    make_journey_template: MakeJourneyTemplate,
    make_template_step: MakeTemplateStep,
) -> None:
    """PRUDENCE. Un brouillon référencé ne devrait pas exister — le front
    délie la ligne d'étape quand le builder se ferme sans promotion. Si on en
    trouve un, quelqu'un l'a délibérément câblé : la FK est en RESTRICT, la
    suppression échouerait de toute façon, et surtout ce n'est plus un
    abandon. On le laisse, et le log le NOMME : un balayage muet qui saute
    des lignes est un balayage qui ment."""
    headers = agent_headers(admin)
    draft = await _draft(client, headers)
    journey = await make_journey_template(agency_id=admin.agency_id)
    step = await make_template_step(template=journey)
    db_session.add(
        StepRequirement(
            step_id=step.id,
            kind="document",
            reference="Mandat",
            scope="principal",
            position=0,
            signature_required=True,
            document_template_id=uuid.UUID(draft["id"]),
        )
    )
    await db_session.commit()
    await _age(db_session, draft["id"], DRAFT_TTL_HOURS + 1)

    lines: list[str] = []
    with sync_session_local() as sync_db:
        stats = await asyncio.to_thread(sweep_document_template_drafts, sync_db, log=lines.append)
    assert stats == {
        "swept": 0,
        "provider_archived": 0,
        "storage_purged": 0,
        "referenced_skipped": 1,
    }
    db_session.expire_all()
    assert (await db_session.execute(select(DocumentTemplate))).scalars().one() is not None
    assert any("RÉFÉRENCÉ" in line for line in lines)


async def test_dry_run_counts_without_deleting(
    client: AsyncClient,
    admin: Agent,
    agent_headers: AuthHeaders,
    fake_provider: FakeProvider,
    db_session,
    sync_session_local: sessionmaker[Session],
) -> None:
    """Le mode à blanc du wrapper de jobs : il COMPTE ce qu'il emporterait,
    et n'emporte rien — ni la ligne, ni le PDF, ni le template provider."""
    headers = agent_headers(admin)
    draft = await _draft(client, headers)
    await _age(db_session, draft["id"], DRAFT_TTL_HOURS + 1)

    stats = await asyncio.to_thread(_sweep, sync_session_local, dry_run=True)
    assert stats["swept"] == 1
    assert stats["provider_archived"] == 0 and stats["storage_purged"] == 0
    db_session.expire_all()
    assert (await db_session.execute(select(DocumentTemplate))).scalars().one() is not None
    assert fake_provider.archive_calls == []


def test_the_janitor_is_wired_into_the_scheduler() -> None:
    """Un job qui n'est pas dans le registre ne tourne jamais — et son
    absence ne se voit nulle part ailleurs qu'ici."""
    from src.core.scheduler import JOB_REGISTRY
    from src.jobs.jobs_baseline import DEFAULT_JOB_CONFIGS

    registered = JOB_REGISTRY["sweep_document_template_drafts"]
    assert registered is document_templates_jobs.sweep_document_template_drafts
    assert any(c["job_id"] == "sweep_document_template_drafts" for c in DEFAULT_JOB_CONFIGS)

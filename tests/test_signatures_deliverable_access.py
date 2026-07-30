"""Lot livrables signataire (30/07) — le droit du signataire sur les
livrables de SA signature.

Règle : un MEMBRE voit ET télécharge les livrables système (PDF signé +
dossier de preuve) d'une étape dont il est signataire d'une demande
COMPLÉTÉE — le lien signature_request.case_step_progress_id →
signature_signer.case_person_id, aucun champ ajouté. La branche est
PARTAGÉE liste/téléchargement (jamais l'un sans l'autre). Un membre
non-signataire ne gagne RIEN ; 404 non-révélateur ; le principal
(qui voit tout) est inchangé."""

import uuid

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from shared.models.expat_user import ExpatUser
from shared.models.rbac import Role
from src.core.config import get_settings
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent
from tests.plugins.case_plugin import MakeClientCase
from tests.plugins.expat_plugin import MakeExpatUser
from tests.plugins.signature_plugin import FAKE_AUDIT, FAKE_PDF, FakeProvider
from tests.test_signatures import _document_template, _requests, _signers
from tests.test_signatures_countersign import _sign

pytestmark = pytest.mark.usefixtures("rbac_baseline", "signatures_enabled")


@pytest_asyncio.fixture
async def admin(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    return await make_agent(role=system_roles["admin"])


async def _two_step_signed_case(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    headers: dict[str, str],
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
    fake_provider: FakeProvider,
    prefix: str,
) -> tuple[uuid.UUID, str, str]:
    """(case_id, progress1_id, progress2_id) — étape 1 : « Statuts »
    each_person (principal + membre signataires) ; étape 2 : « Bail »
    principal SEUL. Les deux activées et TOUTES signatures posées
    (webhooks) → les livrables système existent sur les deux étapes."""
    journey = (await client.post("/journeys", headers=headers, json={"name": "T"})).json()
    step1 = (
        await client.post(
            f"/journeys/{journey['id']}/steps",
            headers=headers,
            json={"name": "Contrat", "validated_by_type": "none"},
        )
    ).json()
    step2 = (
        await client.post(
            f"/journeys/{journey['id']}/steps",
            headers=headers,
            json={"name": "Logement", "validated_by_type": "none"},
        )
    ).json()
    tpl2 = await _document_template(client, headers, name=f"Statuts {prefix}", roles=2)
    r = await client.post(
        f"/journeys/{journey['id']}/steps/{step1['id']}/requirements",
        headers=headers,
        json={
            "kind": "document",
            "reference": "Statuts",
            "scope": "each_person",
            "signature_required": True,
            "document_template_id": tpl2["id"],
        },
    )
    assert r.status_code == 201, r.text
    tpl1 = await _document_template(client, headers, name=f"Bail {prefix}", roles=1)
    r = await client.post(
        f"/journeys/{journey['id']}/steps/{step2['id']}/requirements",
        headers=headers,
        json={
            "kind": "document",
            "reference": "Bail",
            "scope": "principal",
            "signature_required": True,
            "document_template_id": tpl1["id"],
        },
    )
    assert r.status_code == 201, r.text

    principal = await make_expat_user(activated=True, email=f"{prefix}-p@example.com")
    case = await make_client_case(
        agency_id=admin.agency_id, principal_expat_user_id=principal.id, owner_agent_id=admin.id
    )
    r = await client.post(
        f"/cases/{case.id}/persons",
        headers=headers,
        json={
            "full_name": "Membre Livrable",
            "relationship": "associate",
            "email": f"{prefix}-m@example.com",
        },
    )
    assert r.status_code == 201, r.text
    r = await client.post(
        f"/cases/{case.id}/journey", headers=headers, json={"journey_template_id": journey["id"]}
    )
    assert r.status_code == 201, r.text
    timeline = r.json()
    progress1, progress2 = timeline[0]["id"], timeline[1]["id"]
    for pid in (progress1, progress2):
        r = await client.patch(
            f"/cases/{case.id}/steps/{pid}", headers=headers, json={"status": "in_progress"}
        )
        assert r.status_code == 200, r.text
    # TOUT LE MONDE signe (3 sièges : 2 sur l'étape 1, 1 sur l'étape 2).
    for request in await _requests(db_session, case.id):
        for signer in await _signers(db_session, request.id):
            await _sign(client, signer.id, f"whsec-{prefix}")
    return case.id, progress1, progress2


@pytest_asyncio.fixture
def _webhook_secret(monkeypatch: pytest.MonkeyPatch):
    def arm(prefix: str) -> None:
        monkeypatch.setenv("DOCUSEAL_WEBHOOK_SECRET", f"whsec-{prefix}")
        get_settings.cache_clear()

    return arm


async def _expat(db: AsyncSession, email_addr: str) -> ExpatUser:
    """Le compte, ACTIVÉ au passage (un membre créé par l'API naît non
    activé — le smoke d'accès exige un login vivant)."""
    from datetime import UTC, datetime

    expat = (await db.execute(select(ExpatUser).where(ExpatUser.email == email_addr))).scalar_one()
    if expat.activated_at is None:
        expat.activated_at = datetime.now(UTC)
        await db.commit()
        await db.refresh(expat)
    return expat


async def test_member_signer_lists_and_downloads_both_deliverables_of_their_step(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
    expat_headers: AuthHeaders,
    fake_provider: FakeProvider,
    give_credits,
    _webhook_secret,
) -> None:
    await give_credits(admin.agency_id, 10)
    _webhook_secret("dlv")
    case_id, progress1, progress2 = await _two_step_signed_case(
        client,
        db_session,
        admin,
        agent_headers(admin),
        make_client_case,
        make_expat_user,
        fake_provider,
        "dlv",
    )
    member = await _expat(db_session, "dlv-m@example.com")
    m_headers = expat_headers(member)
    resp = await client.get(f"/expat/cases/{case_id}/documents", headers=m_headers)
    assert resp.status_code == 200, resp.text
    docs = resp.json()
    system_docs = [d for d in docs if d["uploaded_by_type"] == "system"]
    # SON étape (étape 1) : le PDF signé ET le dossier de preuve — les 2.
    step1_docs = [d for d in system_docs if d["step_name"] == "Contrat"]
    assert len(step1_docs) == 2
    names = sorted(d["filename"] for d in step1_docs)
    # Volet 1 : noms depuis la référence (langue défaut agence, fr) — et le
    # SOURCE VIERGE (statuts.pdf du modèle) n'apparaît jamais en livrable.
    assert names == ["Statuts — preuve de signature.pdf", "Statuts — signé.pdf"]
    assert all(n != "statuts.pdf" for n in (d["filename"] for d in docs))
    # Téléchargement : les OCTETS réels du storage fake, pour les DEUX.
    by_name = {d["filename"]: d for d in step1_docs}
    r = await client.get(
        f"/expat/cases/{case_id}/documents/{by_name['Statuts — signé.pdf']['id']}/download",
        headers=m_headers,
    )
    assert r.status_code == 200, r.text
    assert r.content == FAKE_PDF
    proof_id = by_name["Statuts — preuve de signature.pdf"]["id"]
    r = await client.get(
        f"/expat/cases/{case_id}/documents/{proof_id}/download",
        headers=m_headers,
    )
    assert r.status_code == 200, r.text
    assert r.content == FAKE_AUDIT
    # CROSS-ÉTAPE : l'étape 2 (principal seul signataire) ne lui donne RIEN.
    assert [d for d in system_docs if d["step_name"] == "Logement"] == []
    # Le PRINCIPAL, lui, voit tout — inchangé (2 étapes × 2 livrables).
    principal = await _expat(db_session, "dlv-p@example.com")
    p_docs = (
        await client.get(f"/expat/cases/{case_id}/documents", headers=expat_headers(principal))
    ).json()
    assert len([d for d in p_docs if d["uploaded_by_type"] == "system"]) == 4


async def test_non_signer_member_gains_nothing(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
    expat_headers: AuthHeaders,
    fake_provider: FakeProvider,
    give_credits,
    _webhook_secret,
) -> None:
    """Témoin négatif : un membre du MÊME dossier sans siège de signature
    ne voit ni ne télécharge les livrables — 404 non-révélateur. Et le
    cross-dossier reste un mur (le membre signataire d'un AUTRE dossier)."""
    agency_id = admin.agency_id  # primitif capturé avant les commits expirants
    await give_credits(agency_id, 10)
    headers = agent_headers(admin)
    _webhook_secret("neg")
    case_id, _p1, _p2 = await _two_step_signed_case(
        client, db_session, admin, headers, make_client_case, make_expat_user, fake_provider, "neg"
    )
    # Un membre AJOUTÉ APRÈS les signatures : aucun siège, aucune ligne.
    r = await client.post(
        f"/cases/{case_id}/persons",
        headers=headers,
        json={
            "full_name": "Spectateur",
            "relationship": "associate",
            "email": "neg-x@example.com",
        },
    )
    assert r.status_code == 201, r.text
    await db_session.rollback()
    spectator = await _expat(db_session, "neg-x@example.com")
    s_headers = expat_headers(spectator)
    docs = (await client.get(f"/expat/cases/{case_id}/documents", headers=s_headers)).json()
    assert [d for d in docs if d["uploaded_by_type"] == "system"] == []
    # Téléchargement direct par id : 404 non-révélateur.
    member = await _expat(db_session, "neg-m@example.com")
    m_headers = expat_headers(member)  # capturé AVANT tout commit expirant
    m_docs = (await client.get(f"/expat/cases/{case_id}/documents", headers=m_headers)).json()
    target = next(d for d in m_docs if d["uploaded_by_type"] == "system")
    r = await client.get(
        f"/expat/cases/{case_id}/documents/{target['id']}/download", headers=s_headers
    )
    assert r.status_code == 404
    # CROSS-DOSSIER : le signataire du dossier « neg » ne touche pas les
    # livrables d'un autre dossier (404 sur le case d'abord).
    other_case = await make_client_case(agency_id=agency_id)
    other_case_id = other_case.id
    r = await client.get(
        f"/expat/cases/{other_case_id}/documents/{target['id']}/download",
        headers=m_headers,
    )
    assert r.status_code == 404

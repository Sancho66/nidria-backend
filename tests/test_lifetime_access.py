"""L'ACCÈS À VIE — le cadeau de la plateforme, et sa reprise.

Le lot ne se joue pas sur l'écriture d'un booléen : il se joue sur ses
CONSÉQUENCES, une par une. Ce fichier les tient toutes — le blocage qui
ne tombe plus, la relance qui ne part plus, la bannière qui n'a plus de
date à afficher, le badge qui dit « À vie » au lieu de ranger le cadeau
avec les anomalies — et la réversibilité, parce qu'un cadeau offert par
erreur doit pouvoir se reprendre.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session, sessionmaker

from shared.models.agency import Agency
from shared.models.agent import Agent
from shared.models.rbac import Role
from shared.models.usage import AgencyUsageMilestone, UsageEvent
from src.billing.billing_lock import blocking_reason
from src.nurture.nurture_job import send_trial_nurture
from tests.plugins.agency_plugin import MakeAgency
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent

pytestmark = pytest.mark.usefixtures("rbac_baseline")


@pytest_asyncio.fixture
async def superadmin(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    return await make_agent(role=system_roles["superadmin"])


@pytest_asyncio.fixture
async def admin(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    return await make_agent(role=system_roles["admin"])


async def _expire_trial(db: AsyncSession, agency_id, days_ago: int = 3) -> Agency:
    agency = await db.get(Agency, agency_id)
    assert agency is not None
    agency.trial_ends_at = datetime.now(UTC) - timedelta(days=days_ago)
    await db.commit()
    await db.refresh(agency)
    return agency


# --- le geste -------------------------------------------------------------------------


async def test_granting_lifetime_clears_the_deadline_and_unblocks(
    client: AsyncClient,
    db_session: AsyncSession,
    superadmin: Agent,
    admin: Agent,
    agent_headers: AuthHeaders,
) -> None:
    """Une agence dont l'essai est EXPIRÉ (donc bloquée) : le cadeau efface
    l'échéance et lève le blocage, dans le même geste."""
    agency = await _expire_trial(db_session, admin.agency_id)
    assert blocking_reason(agency, now=datetime.now(UTC)) == "trial_expired"

    r = await client.patch(
        f"/agencies/{admin.agency_id}/lifetime-access",
        headers=agent_headers(superadmin),
        json={"lifetime_access": True},
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"lifetime_access": True, "trial_ends_at": None}

    await db_session.refresh(agency)
    assert agency.lifetime_access is True
    assert agency.trial_ends_at is None
    assert blocking_reason(agency, now=datetime.now(UTC)) is None


async def test_lifetime_survives_a_deadline_that_would_come_back(
    db_session: AsyncSession, admin: Agent
) -> None:
    """Le drapeau est LA vérité, l'absence de date sa conséquence. Si une
    échéance passée revenait par un autre chemin, le cadeau tiendrait —
    c'est pour ça que `blocking_reason` teste le drapeau EN TÊTE."""
    agency = await db_session.get(Agency, admin.agency_id)
    assert agency is not None
    agency.lifetime_access = True
    agency.trial_ends_at = datetime.now(UTC) - timedelta(days=90)
    await db_session.commit()
    assert blocking_reason(agency, now=datetime.now(UTC)) is None


async def test_revoking_requires_a_new_deadline_and_restores_the_trial(
    client: AsyncClient,
    db_session: AsyncSession,
    superadmin: Agent,
    admin: Agent,
    agent_headers: AuthHeaders,
) -> None:
    """Reprendre le cadeau : le drapeau tombe et une NOUVELLE échéance se
    pose. Sans durée, refus nommé — rendre l'ancienne date ressusciterait
    souvent un essai déjà expiré, donc un blocage que personne n'a décidé."""
    headers = agent_headers(superadmin)
    await client.patch(
        f"/agencies/{admin.agency_id}/lifetime-access",
        headers=headers,
        json={"lifetime_access": True},
    )

    r = await client.patch(
        f"/agencies/{admin.agency_id}/lifetime-access",
        headers=headers,
        json={"lifetime_access": False},
    )
    assert r.status_code == 422, r.text
    assert r.json()["code"] == "lifetime.trial_days_required"

    r = await client.patch(
        f"/agencies/{admin.agency_id}/lifetime-access",
        headers=headers,
        json={"lifetime_access": False, "trial_days": 30},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["lifetime_access"] is False
    ends = datetime.fromisoformat(body["trial_ends_at"])
    assert abs((ends - (datetime.now(UTC) + timedelta(days=30))).total_seconds()) < 5

    agency = await db_session.get(Agency, admin.agency_id)
    assert agency is not None
    await db_session.refresh(agency)
    assert agency.lifetime_access is False
    assert blocking_reason(agency, now=datetime.now(UTC)) is None  # essai vivant


async def test_both_gestures_are_traced_in_the_agency_history(
    client: AsyncClient,
    db_session: AsyncSession,
    superadmin: Agent,
    admin: Agent,
    agent_headers: AuthHeaders,
) -> None:
    """Comme les crédits offerts : qui, quand, et ce que ça remplace."""
    headers = agent_headers(superadmin)
    await _expire_trial(db_session, admin.agency_id, days_ago=1)
    await client.patch(
        f"/agencies/{admin.agency_id}/lifetime-access",
        headers=headers,
        json={"lifetime_access": True},
    )
    await client.patch(
        f"/agencies/{admin.agency_id}/lifetime-access",
        headers=headers,
        json={"lifetime_access": False, "trial_days": 14},
    )
    events = (
        (
            await db_session.execute(
                select(UsageEvent)
                .where(
                    UsageEvent.agency_id == admin.agency_id,
                    UsageEvent.event_type.in_(
                        ["agency.lifetime_granted", "agency.lifetime_revoked"]
                    ),
                )
                .order_by(UsageEvent.created_at)
            )
        )
        .scalars()
        .all()
    )
    assert [e.event_type for e in events] == [
        "agency.lifetime_granted",
        "agency.lifetime_revoked",
    ]
    assert all(e.actor_id == superadmin.id for e in events)
    # Le don dit quelle échéance il a effacée ; la reprise, celle qu'elle pose.
    assert events[0].details["previous_trial_ends_at"] is not None
    assert events[0].details["trial_ends_at"] is None
    assert events[1].details["trial_ends_at"] is not None


# --- les garde-fous -------------------------------------------------------------------


async def test_lifetime_refused_while_a_paddle_subscription_lives(
    client: AsyncClient,
    db_session: AsyncSession,
    superadmin: Agent,
    admin: Agent,
    agent_headers: AuthHeaders,
) -> None:
    """Offrir l'app à quelqu'un que Paddle continue de débiter n'est pas un
    cadeau, c'est un bug de facturation : résilier d'abord."""
    agency = await db_session.get(Agency, admin.agency_id)
    assert agency is not None
    agency.billing_mode = "paddle"
    agency.billing_status = "active"
    await db_session.commit()
    r = await client.patch(
        f"/agencies/{admin.agency_id}/lifetime-access",
        headers=agent_headers(superadmin),
        json={"lifetime_access": True},
    )
    assert r.status_code == 422, r.text
    assert r.json()["code"] == "lifetime.paddle_subscription_active"

    # Résilié chez Paddle → le cadeau passe.
    agency = await db_session.get(Agency, admin.agency_id)
    assert agency is not None
    agency.billing_status = "canceled"
    await db_session.commit()
    r = await client.patch(
        f"/agencies/{admin.agency_id}/lifetime-access",
        headers=agent_headers(superadmin),
        json={"lifetime_access": True},
    )
    assert r.status_code == 200, r.text


async def test_extending_the_trial_of_a_lifetime_agency_is_refused(
    client: AsyncClient,
    db_session: AsyncSession,
    superadmin: Agent,
    admin: Agent,
    agent_headers: AuthHeaders,
) -> None:
    """Prolonger poserait une date en laissant le drapeau debout : deux
    vérités en désaccord. Le retour en essai est un geste nommé."""
    headers = agent_headers(superadmin)
    await client.patch(
        f"/agencies/{admin.agency_id}/lifetime-access",
        headers=headers,
        json={"lifetime_access": True},
    )
    r = await client.patch(
        f"/agencies/{admin.agency_id}/trial", headers=headers, json={"extend_days": 10}
    )
    assert r.status_code == 422, r.text
    assert r.json()["code"] == "trial.lifetime_access"


async def test_only_the_platform_can_offer_lifetime_access(
    client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    """Une agence ne s'offre pas l'accès à vie elle-même (gate agency.create)."""
    r = await client.patch(
        f"/agencies/{admin.agency_id}/lifetime-access",
        headers=agent_headers(admin),
        json={"lifetime_access": True},
    )
    assert r.status_code == 403, r.text


# --- les conséquences, une par une ----------------------------------------------------


async def test_the_superadmin_table_says_lifetime_not_unknown(
    client: AsyncClient,
    db_session: AsyncSession,
    superadmin: Agent,
    admin: Agent,
    agent_headers: AuthHeaders,
) -> None:
    """LE point du lot : sans le drapeau, une agence sans échéance tombe
    dans `unknown` — le seau des anomalies de création. Le badge « Essai
    J-15 » devient « À vie », et le filtre « essais qui expirent » ne la
    propose plus."""
    headers = agent_headers(superadmin)
    await _expire_trial(db_session, admin.agency_id, days_ago=1)

    listing = (await client.get("/admin/agencies?page_size=100", headers=headers)).json()
    row = next(a for a in listing["items"] if a["id"] == str(admin.agency_id))
    assert row["status"] == "expired"

    await client.patch(
        f"/agencies/{admin.agency_id}/lifetime-access",
        headers=headers,
        json={"lifetime_access": True},
    )
    listing = (await client.get("/admin/agencies?page_size=100", headers=headers)).json()
    row = next(a for a in listing["items"] if a["id"] == str(admin.agency_id))
    assert row["status"] == "lifetime"
    assert row["lifetime_access"] is True
    assert row["trial_days_remaining"] is None
    assert row["trial_ends_at"] is None

    # Le funnel d'Eric (essais qui expirent) ne doit plus la faire remonter.
    urgent = (
        await client.get("/admin/agencies?trial_expiring_within_days=30", headers=headers)
    ).json()
    assert all(a["id"] != str(admin.agency_id) for a in urgent["items"])


async def test_no_deadline_left_for_the_banner_to_show(
    client: AsyncClient,
    db_session: AsyncSession,
    superadmin: Agent,
    admin: Agent,
    agent_headers: AuthHeaders,
) -> None:
    """La bannière « J-15 » de la sidebar lit `trial_ends_at` du payload
    agence : plus de date, plus de bannière — et plus de blocage annoncé."""
    await _expire_trial(db_session, admin.agency_id, days_ago=1)
    await client.patch(
        f"/agencies/{admin.agency_id}/lifetime-access",
        headers=agent_headers(superadmin),
        json={"lifetime_access": True},
    )
    me = (await client.get("/agencies/me", headers=agent_headers(admin))).json()
    subscription = me.get("subscription") or me
    assert subscription["trial_ends_at"] is None
    assert subscription["is_blocked"] is False
    assert subscription["blocked_reason"] is None


async def test_the_trial_nurture_leaves_a_lifetime_agency_alone(
    client: AsyncClient,
    db_session: AsyncSession,
    sync_session_local: sessionmaker[Session],
    make_agency: MakeAgency,
    superadmin: Agent,
    agent_headers: AuthHeaders,
) -> None:
    """Les relances d'essai partent sur `trial_ends_at NOT NULL` : le cadeau
    l'efface, l'agence quitte le calendrier — aucune relance « votre essai
    se termine » ne peut plus partir à quelqu'un qui n'a plus d'essai.

    L'agence est montée à la forme RÉELLE du job (activée il y a 7 jours,
    ancre `agence_activee` posée) : sans ça elle n'entre pas dans la
    fenêtre d'envoi et le test prouverait une absence déjà vraie."""
    activated_at = datetime.now(UTC) - timedelta(days=7, hours=1)
    agency = await make_agency(
        slug=f"lifetime-nurture-{uuid.uuid4().hex[:8]}",
        trial_ends_at=activated_at + timedelta(days=30),
    )
    db_session.add(
        AgencyUsageMilestone(agency_id=agency.id, key="agence_activee", first_at=activated_at)
    )
    await db_session.commit()

    with sync_session_local() as sync_db:
        before = send_trial_nurture(sync_db, log=lambda _m: None, dry_run=True)
    assert before["in_scope"] >= 1

    await client.patch(
        f"/agencies/{agency.id}/lifetime-access",
        headers=agent_headers(superadmin),
        json={"lifetime_access": True},
    )
    with sync_session_local() as sync_db:
        after = send_trial_nurture(sync_db, log=lambda _m: None, dry_run=True)
    assert after["in_scope"] == before["in_scope"] - 1


# --- l'onglet Abonnement de l'agence ---------------------------------------------------


async def test_the_subscription_tab_shows_the_gift_instead_of_selling(
    client: AsyncClient,
    db_session: AsyncSession,
    superadmin: Agent,
    admin: Agent,
    agent_headers: AuthHeaders,
) -> None:
    """L'onglet Abonnement dérive son écran du code 409. Sans code dédié,
    une agence à vie tombait dans `not_paddle_managed` — l'écran de
    CONVERSION : on proposait de s'abonner à qui l'app a été offerte.

    Et le mur vaut AUSSI au checkout, pour une raison d'argent : un
    checkout préparé plus tôt ou un appel direct encaisserait un paiement
    que cette agence ne doit pas. Le refus est au serveur, pas à l'écran."""
    h = agent_headers(admin)
    before = await client.get("/billing/subscription", headers=h)
    assert before.status_code == 409
    assert before.json()["code"] == "billing.not_paddle_managed"  # l'essai

    await client.patch(
        f"/agencies/{admin.agency_id}/lifetime-access",
        headers=agent_headers(superadmin),
        json={"lifetime_access": True},
    )

    state = await client.get("/billing/subscription", headers=h)
    assert state.status_code == 409
    assert state.json()["code"] == "billing.lifetime_access"

    checkout = await client.post(
        "/billing/checkout", headers=h, json={"plan": "cabinet", "billing_cycle": "mensuel"}
    )
    assert checkout.status_code == 409
    assert checkout.json()["code"] == "billing.lifetime_access"

    # Repris : l'agence retrouve son écran d'essai, et peut de nouveau payer.
    await client.patch(
        f"/agencies/{admin.agency_id}/lifetime-access",
        headers=agent_headers(superadmin),
        json={"lifetime_access": False, "trial_days": 30},
    )
    again = await client.get("/billing/subscription", headers=h)
    assert again.status_code == 409
    assert again.json()["code"] == "billing.not_paddle_managed"

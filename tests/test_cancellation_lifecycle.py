"""Constat 10/08 — LE CYCLE DE VIE DE L'ANNULATION D'ABONNEMENT.

Grave le comportement RÉEL, bout en bout, des cinq questions du constat :

(1) le geste : `POST /billing/subscription/cancel`, depuis le produit, à la
    FIN DE PÉRIODE PAYÉE (l'immédiat n'est jamais exposé) ;
(2) la période payée : rien ne change tant qu'elle court — l'agence n'est
    pas bloquée, les membres écrivent, les sièges vivent ;
(3) à l'échéance (Paddle pose `canceled`) : le MÊME mur read-only que
    l'essai expiré, par MÉTHODE — personne n'est désactivé, personne
    n'écrit, tout reste lisible, et le plafond retombe à 3 ;
(4) la reprise avant échéance : tout repart, et une composition inchangée
    ne pousse RIEN (pas de re-facturation) ;
(5) la décence : sur une agence bloquée, l'EXPORT (lecture) passe — on ne
    retient personne en otage de ses propres données.
"""

import json
import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agency import Agency
from shared.models.agent import Agent
from shared.models.rbac import Role
from src.billing import paddle_client
from src.core.config import get_settings
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent

pytestmark = pytest.mark.usefixtures("rbac_baseline")

PRICE_IDS = {
    "cabinet_mensuel": "pri_base_cab_m",
    "seat_cabinet_mensuel": "pri_seat_cab_m",
    "seat_reader_mensuel": "pri_seat_reader_m",
}


@pytest.fixture(autouse=True)
def paddle_settings(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("PADDLE_ENV", "sandbox")
    monkeypatch.setenv("PADDLE_API_KEY", "test-api-key")
    monkeypatch.setenv("PADDLE_PRICE_IDS", json.dumps(PRICE_IDS))
    monkeypatch.setenv("BILLING_CHECKOUT_ENABLED", "true")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest_asyncio.fixture
async def admin(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    return await make_agent(role=system_roles["admin"])


async def _live_subscription(
    db: AsyncSession, agency_id: uuid.UUID, *, status: str = "active"
) -> Agency:
    agency = await db.get(Agency, agency_id)
    assert agency is not None
    agency.plan = "cabinet"
    agency.billing_cycle = "mensuel"
    agency.converted_at = datetime.now(UTC)
    agency.billing_mode = "paddle"
    agency.billing_status = status
    agency.paddle_subscription_id = "sub_cancel_lifecycle"
    await db.commit()
    return agency


# --- (1) le geste : depuis le produit, à la fin de période ------------------------------


async def test_cancel_is_a_product_gesture_at_period_end(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """L'agence annule DEPUIS LE PRODUIT (pas de détour par Eric ni par le
    portail Paddle) — et le back n'expose QUE la fin de période : la date
    rendue est l'échéance, le client garde le mois qu'il a payé."""
    await _live_subscription(db_session, admin.agency_id)
    ends_at = "2026-09-01T00:00:00Z"
    cancel = AsyncMock(
        return_value={"scheduled_change": {"action": "cancel", "effective_at": ends_at}}
    )
    monkeypatch.setattr(paddle_client.PaddleClient, "cancel_subscription_at_period_end", cancel)

    response = await client.post("/billing/subscription/cancel", headers=agent_headers(admin))
    assert response.status_code == 200, response.text
    assert response.json()["ends_at"].startswith("2026-09-01")
    cancel.assert_awaited_once()  # le geste part vers Paddle, en fin de période


# --- (2) la période payée : rien ne change ---------------------------------------------


async def test_paid_period_keeps_everything_running(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
) -> None:
    """Annulation PROGRAMMÉE, période en cours : le statut Paddle reste
    `active`, donc AUCUN mur — les membres travaillent, l'agence n'est pas
    plafonnée, les sièges vivent. Le blocage n'arrive qu'à l'échéance."""
    await _live_subscription(db_session, admin.agency_id)
    for i in range(2):  # 3 gestionnaires : au-delà de l'inclus d'aucun plan
        await make_agent(
            role=system_roles["member"], agency_id=admin.agency_id, email=f"m{i}@example.com"
        )
    headers = agent_headers(admin)

    written = await client.post("/journeys", headers=headers, json={"name": "Pendant la période"})
    assert written.status_code == 201, written.text  # on écrit toujours

    subscription = (await client.get("/agencies/me", headers=headers)).json()["subscription"]
    assert subscription["is_blocked"] is False and subscription["blocked_reason"] is None
    assert subscription["seats"]["max"] is None  # aucun plafond : l'abonnement vit
    assert subscription["seats"]["members"] == 3


# --- (3) à l'échéance : le mur, et personne de désactivé -------------------------------


async def test_at_term_the_wall_falls_but_nobody_is_deactivated(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
) -> None:
    """À l'échéance, Paddle pose `canceled` : le MÊME mur read-only que
    l'essai expiré, par MÉTHODE (toute écriture 403, toute lecture 200).
    Les membres au-delà des inclus ne sont ni supprimés ni désactivés —
    ils deviennent lecteurs DE FAIT, le temps de repayer. Le plafond
    retombe à 3 : on ne peut plus INVITER, mais rien n'est détruit."""
    await _live_subscription(db_session, admin.agency_id, status="canceled")
    for i in range(2):
        await make_agent(
            role=system_roles["member"], agency_id=admin.agency_id, email=f"m{i}@example.com"
        )
    headers = agent_headers(admin)

    # Écritures : 403 avec la raison NOMMÉE `canceled` (pas `trial_expired`).
    for method, url, body in (
        ("POST", "/journeys", {"name": "Après l'échéance"}),
        ("PATCH", "/agencies/me", {"name": "Renommée"}),
        ("POST", "/cases", {"origin_country": "FR", "dest_country": "PY"}),
    ):
        refused = await client.request(method, url, headers=headers, json=body)
        assert refused.status_code == 403, (url, refused.text)
        assert refused.json()["code"] == "billing.subscription_required"
        assert refused.json()["params"]["reason"] == "canceled"

    # Lectures : tout reste visible.
    for url in ("/cases", "/journeys", "/agencies/me/members", "/client-profiles"):
        readable = await client.get(url, headers=headers)
        assert readable.status_code == 200, (url, readable.text)

    # Le roster est INTACT : personne désactivé, la donnée entière.
    members = (await client.get("/agencies/me/members", headers=headers)).json()
    assert len(members) == 3
    assert all(m["deactivated_at"] is None for m in members)

    subscription = (await client.get("/agencies/me", headers=headers)).json()["subscription"]
    assert subscription["is_blocked"] is True
    assert subscription["blocked_reason"] == "canceled"
    assert subscription["seats"]["max"] == 3  # le plafond d'essai revient
    assert subscription["plan"] == "cabinet"  # le plan reste un fait historique


async def test_dead_subscription_pushes_nothing_to_paddle(
    db_session: AsyncSession,
    admin: Agent,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Une sub morte ne reçoit plus rien : le sync de sièges s'arrête net
    (le re-abonnement re-dérivera tout depuis le checkout)."""
    from src.billing.billing_manager import BillingManager

    await _live_subscription(db_session, admin.agency_id, status="canceled")
    push = AsyncMock(return_value={})
    monkeypatch.setattr(paddle_client.PaddleClient, "update_subscription_items", push)

    await BillingManager(db_session).sync_seat_quantity(admin.agency_id, increase=False)
    assert push.await_count == 0


# --- (4) la reprise : tout repart, sans re-facturation ---------------------------------


async def test_resume_restores_everything_without_double_billing(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """La reprise avant échéance : le geste efface l'annulation programmée
    et, la composition n'ayant pas bougé, ne pousse RIEN — aucune
    re-facturation. (Le rattrapage de quantité, lui, est gravé par les
    tests test_resume_catches_up_a_missed_seat_up/down.)"""
    from src.billing.billing_manager import BillingManager

    await _live_subscription(db_session, admin.agency_id)
    live = {
        "id": "sub_cancel_lifecycle",
        "status": "active",
        "scheduled_change": None,
        "items": [{"price": {"id": "pri_base_cab_m"}, "quantity": 1}],  # billed 0 = l'écho juste
        "current_billing_period": {"ends_at": "2026-09-01T00:00:00Z"},
        "next_billed_at": "2026-09-01T00:00:00Z",
    }
    monkeypatch.setattr(
        paddle_client.PaddleClient, "remove_scheduled_change", AsyncMock(return_value=live)
    )
    monkeypatch.setattr(BillingManager, "_fetch_subscription", AsyncMock(return_value=live))
    push = AsyncMock(return_value={})
    monkeypatch.setattr(paddle_client.PaddleClient, "update_subscription_items", push)
    headers = agent_headers(admin)

    resumed = await client.post("/billing/subscription/resume", headers=headers)
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["scheduled_cancel_at"] is None  # l'annulation est effacée
    assert push.await_count == 0  # composition inchangée → AUCUN push, aucune re-facture

    # Et l'agence écrit de nouveau (elle n'avait d'ailleurs jamais cessé).
    written = await client.post("/journeys", headers=headers, json={"name": "Après reprise"})
    assert written.status_code == 201, written.text


# --- (5) la décence : l'export passe le mur --------------------------------------------


async def test_blocked_agency_can_still_export_its_data(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    make_client_case,
) -> None:
    """LE POINT DE DÉCENCE : sur une agence bloquée (abonnement annulé),
    l'export d'un dossier — un GET — traverse le mur, qui ne ferme que
    les écritures. On ne retient personne en otage de ses données."""
    case = await make_client_case(agency_id=admin.agency_id)
    await _live_subscription(db_session, admin.agency_id, status="canceled")
    headers = agent_headers(admin)

    exported = await client.get(f"/cases/{case.id}/export", headers=headers)
    assert exported.status_code == 200, exported.text
    assert exported.headers["content-type"] == "application/pdf"
    assert exported.content[:4] == b"%PDF"  # un vrai PDF, sur une agence bloquée

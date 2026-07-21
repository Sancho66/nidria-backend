"""Parrainage automatisé — la machine complète, Paddle mocké partout.

La table referral_credit est LA vérité ; le discount Paddle est
l'exécution. Barème v2 (décidé 17/07) : un palier du PRÉSENT — le NOMBRE
de crédits actifs le fixe (1=20, 2=30, 3=40, 4+=50 plafonné), il monte à
chaque conversion et BAISSE dès qu'un crédit expire. Le posé s'évalue AU
PROCHAIN PRÉLÈVEMENT (jamais de facture surprise), borné à la première
expiration parmi les comptés par maximum_recurring_intervals (Paddle
s'arrête seul — vérifié au spike), le lazy sur transaction.completed
re-pose le palier suivant.
Grant dans apply_conversion (le geste unique :
manuel ET Paddle déclenchent), dormants activés à la conversion du
parrain, re-souscription re-pose, churn du filleul sans effet, discount
étranger jamais écrasé."""

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agency import Agency
from shared.models.agent import Agent
from shared.models.rbac import Role
from shared.models.referral import ReferralCredit
from src.core.config import get_settings
from src.referral.referral_manager import ReferralManager, _add_months, _cycles_until
from tests.plugins.agency_plugin import MakeAgency
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent
from tests.test_billing_paddle import PRICE_IDS, SECRET, _envelope, _post

pytestmark = pytest.mark.usefixtures("rbac_baseline")

NOW = datetime(2026, 7, 17, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def referral_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("PADDLE_ENV", "sandbox")
    monkeypatch.setenv("PADDLE_API_KEY", "test-api-key")
    monkeypatch.setenv("PADDLE_WEBHOOK_SECRET", SECRET)
    monkeypatch.setenv("PADDLE_PRICE_IDS", json.dumps(PRICE_IDS))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest_asyncio.fixture
async def admin(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    return await make_agent(role=system_roles["admin"])


@pytest_asyncio.fixture
async def superadmin(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    return await make_agent(role=system_roles["superadmin"], email="root-ref@platform.io")


def _mock_paddle(
    monkeypatch: pytest.MonkeyPatch,
    *,
    sub: dict[str, Any] | None = None,
    discount: dict[str, Any] | None = None,
) -> dict[str, AsyncMock]:
    from src.billing import paddle_client

    sub = sub or {
        "id": "sub_ref",
        "status": "active",
        "next_billed_at": "2026-08-01T00:00:00Z",
        "items": [],
        "discount": None,
    }
    mocks = {
        "get_subscription": AsyncMock(return_value=sub),
        "get_discount": AsyncMock(return_value=discount or {}),
        "create_discount": AsyncMock(return_value={"id": "dsc_new"}),
        "set_subscription_discount": AsyncMock(return_value={}),
        "archive_discount": AsyncMock(return_value={}),
    }
    for name, mock in mocks.items():
        monkeypatch.setattr(paddle_client.PaddleClient, name, mock)
    return mocks


async def _make_paddle_active(db: AsyncSession, agency_id: uuid.UUID, sub_id: str = "sub_ref"):
    await db.execute(
        update(Agency)
        .where(Agency.id == agency_id)
        .values(
            billing_mode="paddle",
            paddle_subscription_id=sub_id,
            billing_status="active",
            plan="cabinet",
            billing_cycle="mensuel",
            converted_at=datetime.now(UTC),
        )
    )
    await db.commit()


async def _credit(
    db: AsyncSession,
    referrer_id: uuid.UUID,
    *,
    months_left: int = 6,
    rate: int = 20,
) -> ReferralCredit:
    row = ReferralCredit(
        referrer_agency_id=referrer_id,
        referred_agency_id=(await _throwaway_agency(db)),
        granted_at=datetime.now(UTC) - timedelta(days=30),
        expires_at=datetime.now(UTC) + timedelta(days=30 * months_left),
        rate=rate,
    )
    db.add(row)
    await db.commit()
    return row


async def _throwaway_agency(db: AsyncSession) -> uuid.UUID:
    agency = Agency(
        name=f"T {uuid.uuid4().hex[:6]}", slug=f"t-{uuid.uuid4().hex[:10]}", settings={}
    )
    db.add(agency)
    await db.flush()
    return agency.id


# --- helpers purs -----------------------------------------------------------------------


def test_add_months_clamps_end_of_month() -> None:
    assert _add_months(datetime(2026, 1, 31, tzinfo=UTC), 1) == datetime(2026, 2, 28, tzinfo=UTC)
    assert _add_months(datetime(2026, 7, 17, tzinfo=UTC), 12) == datetime(2027, 7, 17, tzinfo=UTC)


def test_cycles_until_counts_billings_before_boundary() -> None:
    nxt = datetime(2026, 8, 1, tzinfo=UTC)
    # mensuel : facturations 01/08, 01/09, 01/10 avant une frontiere au 15/10
    assert _cycles_until(nxt, datetime(2026, 10, 15, tzinfo=UTC), "mensuel") == 3
    # annuel : une seule facturation dans les 12 mois
    assert _cycles_until(nxt, datetime(2027, 7, 17, tzinfo=UTC), "annuel") == 1
    # frontiere avant la prochaine facture : plancher 1 (le cycle entame est accorde)
    assert _cycles_until(nxt, datetime(2026, 7, 20, tzinfo=UTC), "mensuel") == 1


# --- attribution au wizard --------------------------------------------------------------


async def test_wizard_generates_code_and_attributes_referral(
    client: AsyncClient,
    db_session: AsyncSession,
    superadmin: Agent,
    agent_headers: AuthHeaders,
) -> None:
    sh = agent_headers(superadmin)
    first = await client.post(
        "/agencies",
        headers=sh,
        json={
            "name": "Marraine SA",
            "admin_email": "marraine@x.io",
            "admin_first_name": "M",
            "admin_last_name": "A",
            "sectors": ["consulting"],
        },
    )
    assert first.status_code in (200, 201), first.text
    code = first.json()["agency"]["referral_code"]
    assert code and code.startswith("NID-") and len(code) == 10

    second = await client.post(
        "/agencies",
        headers=sh,
        json={
            "name": "Filleule SA",
            "admin_email": "filleule@x.io",
            "admin_first_name": "F",
            "admin_last_name": "A",
            "sectors": ["consulting"],
            "referral_code": code.lower(),  # normalisation: la saisie humaine pardonne
        },
    )
    assert second.status_code in (200, 201), second.text
    db_session.expire_all()
    referred = await db_session.get(Agency, uuid.UUID(second.json()["agency"]["id"]))
    referrer = await db_session.get(Agency, uuid.UUID(first.json()["agency"]["id"]))
    assert referred is not None and referrer is not None
    assert referred.referred_by_agency_id == referrer.id

    unknown = await client.post(
        "/agencies",
        headers=sh,
        json={
            "name": "Perdue SA",
            "admin_email": "perdue@x.io",
            "admin_first_name": "P",
            "admin_last_name": "A",
            "sectors": ["consulting"],
            "referral_code": "NID-ZZZZZZ",
        },
    )
    assert unknown.status_code == 422
    assert unknown.json()["code"] == "referral.code_unknown"


# --- le grant a la conversion (Paddle ET manuel) ---------------------------------------


async def test_paddle_conversion_grants_credit_poses_discount_and_emails(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Le parrain : converti paddle, sub vivante.
    referrer = admin
    await _make_paddle_active(db_session, referrer.agency_id)
    # Le filleul : attribue au parrain, converti par webhook activated.
    godchild_admin = await make_agent(role=system_roles["admin"], email="fil@x.io")
    await db_session.execute(
        update(Agency)
        .where(Agency.id == godchild_admin.agency_id)
        .values(referred_by_agency_id=referrer.agency_id)
    )
    await db_session.commit()
    mocks = _mock_paddle(monkeypatch)
    sent: list[tuple] = []
    monkeypatch.setattr("src.referral.referral_manager.send_email", lambda *a, **k: sent.append(a))

    resp = await _post(
        client,
        _envelope(
            "subscription.activated",
            agency_id=godchild_admin.agency_id,
            subscription_id="sub_fil",
        ),
    )
    assert resp.json()["status"] == "processed"

    credit = (
        await db_session.execute(
            select(ReferralCredit).where(
                ReferralCredit.referred_agency_id == godchild_admin.agency_id
            )
        )
    ).scalar_one()
    assert credit.referrer_agency_id == referrer.agency_id and credit.rate == 20
    assert (credit.expires_at - credit.granted_at).days >= 360  # +12 mois
    # Le discount 20% pose sur la sub du parrain, borne a la frontiere.
    mocks["create_discount"].assert_awaited_once()
    kwargs = mocks["create_discount"].await_args.kwargs
    assert kwargs["rate"] == 20 and kwargs["maximum_recurring_intervals"] >= 1
    mocks["set_subscription_discount"].assert_awaited_once_with("sub_ref", "dsc_new")
    # L'email au parrain est parti (mock, zero reseau).
    assert len(sent) == 1 and sent[0][0] == referrer.email


async def test_manual_conversion_triggers_the_same_grant(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    superadmin: Agent,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Le geste unique : un filleul converti PAR ERIC credite aussi."""
    godchild_admin = await make_agent(role=system_roles["admin"], email="fil-man@x.io")
    await db_session.execute(
        update(Agency)
        .where(Agency.id == godchild_admin.agency_id)
        .values(referred_by_agency_id=admin.agency_id)
    )
    await db_session.commit()
    _mock_paddle(monkeypatch)
    monkeypatch.setattr("src.referral.referral_manager.send_email", lambda *a, **k: None)

    resp = await client.patch(
        f"/agencies/{godchild_admin.agency_id}/subscription",
        headers=agent_headers(superadmin),
        json={"plan": "cabinet", "billing_cycle": "mensuel"},
    )
    assert resp.status_code == 200, resp.text
    credit = (
        await db_session.execute(
            select(ReferralCredit).where(
                ReferralCredit.referred_agency_id == godchild_admin.agency_id
            )
        )
    ).scalar_one()
    assert credit.referrer_agency_id == admin.agency_id


async def test_self_referral_belt_never_credits(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    superadmin: Agent,
    agent_headers: AuthHeaders,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await db_session.execute(
        update(Agency)
        .where(Agency.id == admin.agency_id)
        .values(referred_by_agency_id=admin.agency_id)  # forge: soi-meme
    )
    await db_session.commit()
    _mock_paddle(monkeypatch)
    resp = await client.patch(
        f"/agencies/{admin.agency_id}/subscription",
        headers=agent_headers(superadmin),
        json={"plan": "cabinet", "billing_cycle": "mensuel"},
    )
    assert resp.status_code == 200
    count = (
        (
            await db_session.execute(
                select(ReferralCredit).where(ReferralCredit.referrer_agency_id == admin.agency_id)
            )
        )
        .scalars()
        .all()
    )
    assert count == []


# --- la machine de recalcul -------------------------------------------------------------


async def _grant_for_new_godchild(db: AsyncSession, referrer_id: uuid.UUID) -> ReferralCredit:
    """Un filleul converti de plus : agence attribuee au parrain + grant."""
    godchild = Agency(
        name=f"G {uuid.uuid4().hex[:6]}",
        slug=f"gc-{uuid.uuid4().hex[:10]}",
        settings={},
        referred_by_agency_id=referrer_id,
    )
    db.add(godchild)
    await db.flush()
    assert await ReferralManager(db).grant_on_conversion(godchild) is True
    await db.commit()
    return (
        await db.execute(
            select(ReferralCredit).where(ReferralCredit.referred_agency_id == godchild.id)
        )
    ).scalar_one()


async def test_tier_climbs_with_each_active_credit(db_session: AsyncSession, admin: Agent) -> None:
    """La montee 20/30/40/50/50 : le palier suit le NOMBRE d'actifs
    (tous vivants ici), plafond 50."""
    rates = [(await _grant_for_new_godchild(db_session, admin.agency_id)).rate for _ in range(5)]
    assert rates == [20, 30, 40, 50, 50]


async def test_expiry_drops_the_tier(
    db_session: AsyncSession, admin: Agent, monkeypatch: pytest.MonkeyPatch
) -> None:
    """2 actifs = 30 ; le 1er expire -> le pose retombe a 20, et un
    NOUVEAU converti prend le palier du PRESENT (30, pas 40 : plus de
    rang, que des vivants)."""
    first = await _grant_for_new_godchild(db_session, admin.agency_id)  # 20
    await _grant_for_new_godchild(db_session, admin.agency_id)  # 30
    await _make_paddle_active(db_session, admin.agency_id)
    mocks = _mock_paddle(monkeypatch)
    agency = await db_session.get(Agency, admin.agency_id)
    assert agency is not None
    await ReferralManager(db_session).recompute_discount(agency)
    assert mocks["create_discount"].await_args.kwargs["rate"] == 30  # 2 actifs

    await db_session.execute(
        update(ReferralCredit)
        .where(ReferralCredit.id == first.id)
        .values(expires_at=datetime.now(UTC) - timedelta(days=1))
    )
    await db_session.commit()
    mocks["create_discount"].reset_mock()
    await ReferralManager(db_session).recompute_discount(agency)
    kwargs = mocks["create_discount"].await_args.kwargs
    assert kwargs["rate"] == 20  # la baisse : 1 seul vivant
    assert kwargs["custom_data"]["referral_key"].startswith("20:")

    third = await _grant_for_new_godchild(db_session, admin.agency_id)
    assert third.rate == 30  # le palier du present (2 vivants), pas un rang


async def test_drop_is_posed_before_the_next_invoice(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """La baisse ne surprend JAMAIS la facture : un credit meurt AVANT le
    prochain prelevement -> le lazy tick du prelevement courant pose DEJA
    le palier d'en dessous pour le suivant (sequence prouvee : create du
    20, remplacement, archive de l'ancien 30)."""
    aid = admin.agency_id
    await _make_paddle_active(db_session, aid, sub_id="sub_drop")
    # meurt dans 5 jours, AVANT le next_billed du 01/08 : ne compte plus
    dying = await _credit(db_session, aid, months_left=0)
    await db_session.execute(
        update(ReferralCredit)
        .where(ReferralCredit.id == dying.id)
        .values(expires_at=datetime.now(UTC) + timedelta(days=5))
    )
    await db_session.commit()
    await _credit(db_session, aid, months_left=10)  # survivant
    mocks = _mock_paddle(
        monkeypatch,
        sub={
            "id": "sub_drop",
            "status": "active",
            "next_billed_at": "2026-08-01T00:00:00Z",
            "items": [],
            "discount": {"id": "dsc_30"},
        },
        discount={
            "id": "dsc_30",
            "custom_data": {"referral_agency_id": str(aid), "referral_key": "30:2026-07-22"},
        },
    )
    resp = await _post(
        client, _envelope("transaction.completed", agency_id=aid, subscription_id="sub_drop")
    )
    assert resp.status_code == 200
    kwargs = mocks["create_discount"].await_args.kwargs
    assert kwargs["rate"] == 20  # le palier du PROCHAIN prelevement
    assert kwargs["custom_data"]["referral_key"].startswith("20:")
    assert kwargs["maximum_recurring_intervals"] >= 1
    mocks["set_subscription_discount"].assert_awaited_with("sub_drop", "dsc_new")
    mocks["archive_discount"].assert_awaited_once_with("dsc_30")  # l'ancien 30 archive


async def test_granted_email_carries_the_real_rate(
    db_session: AsyncSession, admin: Agent, monkeypatch: pytest.MonkeyPatch
) -> None:
    """2e parrainage -> l'email dit -30 %, jamais un taux en dur."""
    await _grant_for_new_godchild(db_session, admin.agency_id)  # rang 1
    second = await _grant_for_new_godchild(db_session, admin.agency_id)  # rang 2 = 30
    sent: list[tuple] = []
    monkeypatch.setattr("src.referral.referral_manager.send_email", lambda *a, **k: sent.append(a))
    referrer = await db_session.get(Agency, admin.agency_id)
    referred = await db_session.get(Agency, second.referred_agency_id)
    assert referrer is not None and referred is not None
    await ReferralManager(db_session)._notify_referrer(referrer, referred)
    assert len(sent) == 1
    subject = sent[0][1]
    assert "30" in subject and "20" not in subject


async def test_intervals_stop_at_the_first_expiry(
    db_session: AsyncSession, admin: Agent, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Le pose = palier du nombre d'actifs, borne a la PREMIERE expiration
    parmi eux (la ou n baisse)."""
    await _make_paddle_active(db_session, admin.agency_id)
    await _credit(db_session, admin.agency_id, months_left=2)  # la frontiere
    await _credit(db_session, admin.agency_id, months_left=10)
    mocks = _mock_paddle(monkeypatch)
    agency = await db_session.get(Agency, admin.agency_id)
    await ReferralManager(db_session).recompute_discount(agency)
    kwargs = mocks["create_discount"].await_args.kwargs
    assert kwargs["rate"] == 30  # 2 actifs au prochain prelevement
    # frontiere ~60 j apres now, next_billed 01/08 : 1 a 3 facturations.
    assert 1 <= kwargs["maximum_recurring_intervals"] <= 3


async def test_dormant_credits_activate_on_referrer_conversion(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Le parrain en ESSAI accumule ; sa propre conversion pousse."""
    await _credit(db_session, admin.agency_id)  # credit dormant (pas de sub)
    mocks = _mock_paddle(monkeypatch)
    agency = await db_session.get(Agency, admin.agency_id)
    await ReferralManager(db_session).recompute_discount(agency)
    mocks["create_discount"].assert_not_awaited()  # dormant: rien a executer

    # Sa conversion (webhook activated) reveille les credits.
    resp = await _post(client, _envelope("subscription.activated", agency_id=admin.agency_id))
    assert resp.json()["status"] == "processed"
    assert mocks["create_discount"].await_args.kwargs["rate"] == 20
    mocks["set_subscription_discount"].assert_awaited()


async def test_resubscription_reposes_active_credits(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    aid = admin.agency_id
    await _credit(db_session, aid)
    t0 = datetime.now(UTC) - timedelta(days=60)
    mocks = _mock_paddle(monkeypatch)
    await _post(client, _envelope("subscription.activated", agency_id=aid, occurred_at=t0))
    await _post(
        client,
        _envelope("subscription.canceled", agency_id=aid, occurred_at=t0 + timedelta(days=30)),
    )
    mocks["create_discount"].reset_mock()
    resub = await _post(
        client, _envelope("subscription.activated", agency_id=aid, subscription_id="sub_new")
    )
    assert resub.json()["status"] == "processed"
    mocks["create_discount"].assert_awaited()  # les credits actifs se re-posent


async def test_lazy_tick_reposes_next_tier_and_archives_the_old(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Apres une frontiere, la transaction.completed du parrain re-pose le
    palier suivant (40 -> 20) et archive le discount obsolete."""
    aid = admin.agency_id
    await _make_paddle_active(db_session, aid, sub_id="sub_123")
    await _credit(db_session, aid, months_left=6)  # le palier restant: 20
    # un credit EXPIRE (l'autre moitie du 40 d'hier)
    dead = ReferralCredit(
        referrer_agency_id=aid,
        referred_agency_id=await _throwaway_agency(db_session),
        granted_at=datetime.now(UTC) - timedelta(days=400),
        expires_at=datetime.now(UTC) - timedelta(days=5),
        rate=20,
    )
    db_session.add(dead)
    await db_session.commit()
    mocks = _mock_paddle(
        monkeypatch,
        sub={
            "id": "sub_123",
            "status": "active",
            "next_billed_at": "2026-08-01T00:00:00Z",
            "items": [],
            "discount": {"id": "dsc_old"},
        },
        discount={
            "id": "dsc_old",
            "custom_data": {"referral_agency_id": str(aid), "referral_key": "40:2026-07-12"},
        },
    )

    resp = await _post(
        client,
        _envelope("transaction.completed", agency_id=aid, subscription_id="sub_123"),
    )
    assert resp.status_code == 200
    assert mocks["create_discount"].await_args.kwargs["rate"] == 20  # le palier suivant
    mocks["archive_discount"].assert_awaited_once_with("dsc_old")


async def test_foreign_discount_is_never_clobbered(
    db_session: AsyncSession,
    admin: Agent,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    await _make_paddle_active(db_session, admin.agency_id)
    await _credit(db_session, admin.agency_id)
    mocks = _mock_paddle(
        monkeypatch,
        sub={
            "id": "sub_ref",
            "status": "active",
            "next_billed_at": "2026-08-01T00:00:00Z",
            "items": [],
            "discount": {"id": "dsc_promo_eric"},
        },
        discount={"id": "dsc_promo_eric", "custom_data": {"campaign": "promo"}},
    )
    agency = await db_session.get(Agency, admin.agency_id)
    with caplog.at_level("ERROR"):
        await ReferralManager(db_session).recompute_discount(agency)
    mocks["create_discount"].assert_not_awaited()
    mocks["set_subscription_discount"].assert_not_awaited()
    mocks["archive_discount"].assert_not_awaited()
    assert any("FOREIGN" in r.message for r in caplog.records)


async def test_expired_credits_remove_our_discount(
    db_session: AsyncSession, admin: Agent, monkeypatch: pytest.MonkeyPatch
) -> None:
    aid = admin.agency_id
    await _make_paddle_active(db_session, aid)
    dead = ReferralCredit(
        referrer_agency_id=aid,
        referred_agency_id=await _throwaway_agency(db_session),
        granted_at=datetime.now(UTC) - timedelta(days=400),
        expires_at=datetime.now(UTC) - timedelta(days=5),
        rate=20,
    )
    db_session.add(dead)
    await db_session.commit()
    mocks = _mock_paddle(
        monkeypatch,
        sub={
            "id": "sub_ref",
            "status": "active",
            "next_billed_at": "2026-08-01T00:00:00Z",
            "items": [],
            "discount": {"id": "dsc_ours"},
        },
        discount={
            "id": "dsc_ours",
            "custom_data": {"referral_agency_id": str(aid), "referral_key": "20:2026-07-12"},
        },
    )
    agency = await db_session.get(Agency, aid)
    await ReferralManager(db_session).recompute_discount(agency)
    mocks["set_subscription_discount"].assert_awaited_once_with("sub_ref", None)
    mocks["archive_discount"].assert_awaited_once_with("dsc_ours")


async def test_matching_posed_state_is_a_noop(
    db_session: AsyncSession, admin: Agent, monkeypatch: pytest.MonkeyPatch
) -> None:
    """L'etat pose se LIT, jamais ne se memorise : cle identique = zero appel."""
    aid = admin.agency_id
    await _make_paddle_active(db_session, aid)
    credit = await _credit(db_session, aid, months_left=6)
    key = f"20:{credit.expires_at.date().isoformat()}"
    mocks = _mock_paddle(
        monkeypatch,
        sub={
            "id": "sub_ref",
            "status": "active",
            "next_billed_at": "2026-08-01T00:00:00Z",
            "items": [],
            "discount": {"id": "dsc_ours"},
        },
        discount={
            "id": "dsc_ours",
            "custom_data": {"referral_agency_id": str(aid), "referral_key": key},
        },
    )
    agency = await db_session.get(Agency, aid)
    await ReferralManager(db_session).recompute_discount(agency)
    mocks["create_discount"].assert_not_awaited()
    mocks["set_subscription_discount"].assert_not_awaited()


async def test_godchild_churn_does_not_revoke_the_credit(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _make_paddle_active(db_session, admin.agency_id)
    godchild_admin = await make_agent(role=system_roles["admin"], email="churn@x.io")
    gid = godchild_admin.agency_id
    await db_session.execute(
        update(Agency).where(Agency.id == gid).values(referred_by_agency_id=admin.agency_id)
    )
    await db_session.commit()
    _mock_paddle(monkeypatch)
    monkeypatch.setattr("src.referral.referral_manager.send_email", lambda *a, **k: None)
    await _post(client, _envelope("subscription.activated", agency_id=gid, subscription_id="sub_g"))
    # Le filleul churne.
    await _post(client, _envelope("subscription.canceled", agency_id=gid, subscription_id="sub_g"))
    db_session.expire_all()
    credit = (
        await db_session.execute(
            select(ReferralCredit).where(ReferralCredit.referred_agency_id == gid)
        )
    ).scalar_one()
    assert credit.expires_at > datetime.now(UTC)  # intact: rien ne revoque


# --- the front line (2026-07-17): the POSED discount is readable on the state ---


async def _billing_state(client: AsyncClient, headers: dict[str, str]) -> dict[str, Any]:
    res = await client.get("/billing/subscription", headers=headers)
    assert res.status_code == 200, res.text
    return res.json()


async def test_state_exposes_the_posed_referral_discount(
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    make_agent: MakeAgent,
    make_agency: MakeAgency,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    agency = await make_agency(name="Expose SA", slug="expose-sa")
    admin = await make_agent(agency_id=agency.id, role=system_roles["admin"], email="adm@expose.io")
    await _make_paddle_active(db_session, agency.id, sub_id="sub_refd1")
    _mock_paddle(
        monkeypatch,
        sub={
            "id": "sub_refd1",
            "status": "active",
            "next_billed_at": "2026-08-01T00:00:00Z",
            "items": [],
            "discount": {"id": "dsc_ours_1", "ends_at": "2026-11-01T00:00:00Z"},
        },
        discount={
            "id": "dsc_ours_1",
            "amount": "40",
            "custom_data": {"referral_agency_id": str(agency.id), "referral_key": "40:2026-11-01"},
        },
    )
    state = await _billing_state(client, agent_headers(admin))
    assert state["referral_discount"] == {
        "percent": 40,
        "ends_at": "2026-11-01T00:00:00Z",
    }


async def test_state_never_dresses_a_foreign_discount_as_referral(
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    make_agent: MakeAgent,
    make_agency: MakeAgency,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    agency = await make_agency(name="Promo SA", slug="promo-sa")
    admin = await make_agent(agency_id=agency.id, role=system_roles["admin"], email="adm@promo.io")
    await _make_paddle_active(db_session, agency.id, sub_id="sub_refd2")
    _mock_paddle(
        monkeypatch,
        sub={
            "id": "sub_refd2",
            "status": "active",
            "next_billed_at": "2026-08-01T00:00:00Z",
            "items": [],
            "discount": {"id": "dsc_promo_eric", "ends_at": "2026-09-01T00:00:00Z"},
        },
        # A promo posed by hand: NO referral_key in custom_data.
        discount={"id": "dsc_promo_eric", "amount": "10", "custom_data": {"campaign": "ete"}},
    )
    state = await _billing_state(client, agent_headers(admin))
    assert state["referral_discount"] is None


async def test_state_without_discount_reads_null(
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    make_agent: MakeAgent,
    make_agency: MakeAgency,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    agency = await make_agency(name="Nu SA", slug="nu-sa")
    admin = await make_agent(agency_id=agency.id, role=system_roles["admin"], email="adm@nu.io")
    await _make_paddle_active(db_session, agency.id, sub_id="sub_refd3")
    mocks = _mock_paddle(
        monkeypatch,
        sub={
            "id": "sub_refd3",
            "status": "active",
            "next_billed_at": "2026-08-01T00:00:00Z",
            "items": [],
            "discount": None,
        },
    )
    state = await _billing_state(client, agent_headers(admin))
    assert state["referral_discount"] is None
    mocks["get_discount"].assert_not_awaited()  # zero extra Paddle call


# --- la vue du parrain (GET /agencies/me/referrals) -------------------------------------


async def _godchild(
    db: AsyncSession,
    referrer_id: uuid.UUID,
    *,
    name: str,
    created_at: datetime,
    converted_at: datetime | None = None,
    trial_ends_at: datetime | None = None,
) -> Agency:
    agency = Agency(
        name=name, slug=f"g-{uuid.uuid4().hex[:10]}", settings={}, referred_by_agency_id=referrer_id
    )
    db.add(agency)
    await db.flush()
    await db.execute(
        update(Agency)
        .where(Agency.id == agency.id)
        .values(created_at=created_at, converted_at=converted_at, trial_ends_at=trial_ends_at)
    )
    await db.commit()
    return agency


async def test_referrer_view_lists_three_statuses_sorted(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
) -> None:
    """Les 3 statuts, le credit du converti, le tri (credit actif d'abord,
    puis referred_at desc) — et RIEN de la vie du filleul dans la reponse."""
    now = datetime.now(UTC)
    converted = await _godchild(
        db_session,
        admin.agency_id,
        name="Filleule Convertie",
        created_at=now - timedelta(days=40),  # la plus ancienne, mais creditee
        converted_at=now - timedelta(days=10),
    )
    await _godchild(
        db_session,
        admin.agency_id,
        name="Filleule En Essai",
        created_at=now - timedelta(days=5),
        trial_ends_at=now + timedelta(days=25),
    )
    await _godchild(
        db_session,
        admin.agency_id,
        name="Filleule Echue",
        created_at=now - timedelta(days=20),
        trial_ends_at=now - timedelta(days=2),
    )
    db_session.add(
        ReferralCredit(
            referrer_agency_id=admin.agency_id,
            referred_agency_id=converted.id,
            granted_at=now - timedelta(days=10),
            expires_at=now + timedelta(days=355),
            rate=20,
        )
    )
    await db_session.execute(
        update(Agency).where(Agency.id == admin.agency_id).values(referral_code="NID-TEST01")
    )
    await db_session.commit()

    response = await client.get("/agencies/me/referrals", headers=agent_headers(admin))
    assert response.status_code == 200
    body = response.json()
    assert body["referral_code"] == "NID-TEST01"
    assert body["current_discount"] is None  # agence manuelle : rien de pose
    names = [r["agency_name"] for r in body["referrals"]]
    # credit actif d'abord (malgre son referred_at le plus ancien), puis desc
    assert names == ["Filleule Convertie", "Filleule En Essai", "Filleule Echue"]
    by_name = {r["agency_name"]: r for r in body["referrals"]}
    assert by_name["Filleule Convertie"]["status"] == "converted"
    assert by_name["Filleule En Essai"]["status"] == "trial"
    assert by_name["Filleule Echue"]["status"] == "expired"
    credit = by_name["Filleule Convertie"]["credit"]
    assert credit["active"] is True and credit["granted_at"] and credit["expires_at"]
    # la prochaine echeance ANNONCEE : le seul credit actif expire -> 0 %
    assert body["next_change"]["date"] == credit["expires_at"]
    assert body["next_change"]["percent"] == 0
    assert by_name["Filleule En Essai"]["credit"] is None
    # jamais la vie du filleul : nom, statut, dates de parrainage, c'est tout
    assert set(by_name["Filleule Convertie"]) == {"agency_name", "status", "referred_at", "credit"}


async def test_referrer_view_is_scoped_to_own_godchildren(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    make_agency: MakeAgency,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    now = datetime.now(UTC)
    await _godchild(
        db_session,
        admin.agency_id,
        name="A Moi",
        created_at=now,
        trial_ends_at=now + timedelta(days=9),
    )
    other = await make_agency(name="Autre SA", slug="autre-parrain")
    other_admin = await make_agent(
        agency_id=other.id, role=system_roles["admin"], email="adm@autre-parrain.io"
    )
    mine = (await client.get("/agencies/me/referrals", headers=agent_headers(admin))).json()
    theirs = (await client.get("/agencies/me/referrals", headers=agent_headers(other_admin))).json()
    assert [r["agency_name"] for r in mine["referrals"]] == ["A Moi"]
    assert theirs["referrals"] == []


async def test_referrer_view_requires_agency_manage(
    client: AsyncClient,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    member = await make_agent(role=system_roles["member"], email="membre-ref@agency.io")
    response = await client.get("/agencies/me/referrals", headers=agent_headers(member))
    assert response.status_code == 403


async def test_expired_godchild_has_no_credit(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
) -> None:
    """L'essai echu sans conversion : status expired, credit null — aucune
    ligne referral_credit n'existe (le grant n'a jamais eu lieu)."""
    now = datetime.now(UTC)
    await _godchild(
        db_session,
        admin.agency_id,
        name="Echue Sans Credit",
        created_at=now - timedelta(days=45),
        trial_ends_at=now - timedelta(days=15),
    )
    body = (await client.get("/agencies/me/referrals", headers=agent_headers(admin))).json()
    (entry,) = body["referrals"]
    assert entry["status"] == "expired"
    assert entry["credit"] is None
    assert body["next_change"] is None  # rien d'actif : rien ne changera seul

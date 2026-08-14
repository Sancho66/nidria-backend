"""Alerte INTERNE à chaque inscription autonome d'agence (demande Eric 13/08).

Ce mail part À L'ÉQUIPE, immédiatement, dans la requête d'inscription — à ne
pas confondre avec l'onboarding J+10 min, qui part À L'AGENCE par le
scheduler. Deux mails, deux drapeaux, deux cycles de vie.

Ce que ces tests gravent : l'alerte part UNE fois, aux destinataires
CONFIGURÉS, jamais pour un compte de test, jamais pour une agence créée par
le wizard superadmin, jamais pour un membre invité ensuite — et un échec
d'envoi ne coûte JAMAIS l'inscription."""

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agency import Agency
from shared.models.rbac import Role
from src.core import ratelimit
from src.core.config import get_settings
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent

pytestmark = pytest.mark.usefixtures("rbac_baseline")

RECIPIENTS = ["eric@nidria-interne.test", "alexia@nidria-interne.test"]


@pytest.fixture(autouse=True)
def alert_outbox(monkeypatch: pytest.MonkeyPatch):
    """Capture les envois de l'ALERTE seulement (les mails du signup lui-même
    passent par une autre référence) + code de vérification déterministe."""
    monkeypatch.setattr("src.signup.signup_manager._generate_code", lambda: "123456")
    monkeypatch.setattr("src.signup.signup_manager.send_email", lambda *a, **kw: None)
    box: list[dict] = []
    monkeypatch.setattr(
        "src.signup.signup_alert.send_email",
        lambda to, subject, body, html=None, **kw: box.append(
            {"to": to, "subject": subject, "body": body}
        ),
    )
    ratelimit.reset()
    yield box
    ratelimit.reset()


@pytest.fixture
def alert_on(monkeypatch: pytest.MonkeyPatch):
    """Hors production l'alerte est MUETTE par défaut — on l'arme
    explicitement, par SON interrupteur (plus celui de l'onboarding)."""
    settings = get_settings()
    monkeypatch.setattr(settings, "signup_alert_enabled", True)
    monkeypatch.setattr(settings, "signup_alert_recipients", list(RECIPIENTS))
    return settings


async def _signup(client: AsyncClient, email: str = "neo@agence.io", **overrides):
    assert (await client.post("/signup", json={"email": email, "lang": "fr"})).status_code == 200
    token = (await client.post("/signup/verify", json={"email": email, "code": "123456"})).json()[
        "completion_token"
    ]
    payload = {
        "completion_token": token,
        "agency_name": "Neo Agence",
        "first_name": "Neo",
        "last_name": "Fondateur",
        "password": "MotDePasse1!",
        "language": "fr",
        "sectors": ["legal"],
    }
    payload.update(overrides)
    return await client.post("/signup/complete", json=payload)


# --- le nominal ---------------------------------------------------------------------


async def test_alert_goes_once_to_every_configured_recipient(
    client: AsyncClient, db_session: AsyncSession, alert_outbox: list[dict], alert_on
) -> None:
    assert (await _signup(client)).status_code == 200

    assert sorted(m["to"] for m in alert_outbox) == sorted(RECIPIENTS)
    assert all(m["subject"] == "Nouvelle inscription — Neo Agence" for m in alert_outbox)
    body = alert_outbox[0]["body"]
    assert "Neo Agence" in body
    assert "Neo Fondateur <neo@agence.io>" in body
    assert "UTC" in body  # fuseau explicite, jamais une heure nue
    assert "Langue     : fr" in body
    # La fiche agence : l'URL COMPLÈTE, pas un chemin relatif.
    assert f"{get_settings().frontend_url}/admin/agencies" in body

    agency = (await db_session.execute(select(Agency))).scalars().one()
    assert agency.signup_alert_sent_at is not None


async def test_recipients_come_from_config_not_from_code(
    client: AsyncClient, alert_outbox: list[dict], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ajouter un collègue demain = une variable d'env, pas un déploiement."""
    settings = get_settings()
    monkeypatch.setattr(settings, "signup_alert_enabled", True)
    monkeypatch.setattr(settings, "signup_alert_recipients", ["seule@nidria-interne.test"])
    assert (await _signup(client)).status_code == 200
    assert [m["to"] for m in alert_outbox] == ["seule@nidria-interne.test"]


async def test_empty_recipient_list_sends_nothing(
    client: AsyncClient, alert_outbox: list[dict], monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "signup_alert_enabled", True)
    monkeypatch.setattr(settings, "signup_alert_recipients", [])
    assert (await _signup(client)).status_code == 200
    assert alert_outbox == []


# --- idempotence --------------------------------------------------------------------


async def test_a_replay_never_produces_a_second_alert(
    client: AsyncClient, db_session: AsyncSession, alert_outbox: list[dict], alert_on
) -> None:
    assert (await _signup(client)).status_code == 200
    assert len(alert_outbox) == len(RECIPIENTS)

    from src.signup.signup_alert import notify_signup

    agency = (await db_session.execute(select(Agency))).scalars().one()
    from shared.models.agent import Agent

    admin = (
        (await db_session.execute(select(Agent).where(Agent.agency_id == agency.id)))
        .scalars()
        .first()
    )
    assert admin is not None
    # Le drapeau est posé : rejouer l'appel ne renvoie rien.
    assert await notify_signup(db_session, agency, admin) is False
    assert len(alert_outbox) == len(RECIPIENTS)


# --- les comptes de test : LA MÊME définition que l'onboarding ------------------------


@pytest.mark.parametrize("email", ["equipe@nidria.com", "qa@nidria.app", "eric+test@gmail.com"])
async def test_no_alert_for_an_excluded_test_account(
    client: AsyncClient, alert_outbox: list[dict], alert_on, email: str
) -> None:
    assert (await _signup(client, email=email)).status_code == 200
    assert alert_outbox == []


async def test_the_exclusion_list_is_the_onboarding_one(
    client: AsyncClient, alert_outbox: list[dict], monkeypatch: pytest.MonkeyPatch
) -> None:
    """La définition de « compte de test » reste PARTAGÉE : on ajoute un
    motif à la variable de l'onboarding et l'alerte l'honore aussitôt. C'est
    ELLE, la définition unique — pas l'interrupteur, qui est séparé."""
    settings = get_settings()
    monkeypatch.setattr(settings, "signup_alert_enabled", True)
    monkeypatch.setattr(settings, "signup_alert_recipients", list(RECIPIENTS))
    monkeypatch.setattr(settings, "onboarding_email_excluded_patterns", ["@agence.io"])
    assert (await _signup(client)).status_code == 200
    assert alert_outbox == []


async def test_muted_outside_production_by_default(
    client: AsyncClient, alert_outbox: list[dict], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Un seed local de 20 agences n'envoie AUCUN mail sans que personne
    n'ait à penser à un drapeau."""
    settings = get_settings()
    monkeypatch.setattr(settings, "signup_alert_enabled", None)  # = dérive de l'env
    monkeypatch.setattr(settings, "signup_alert_recipients", list(RECIPIENTS))
    assert (await _signup(client)).status_code == 200
    assert alert_outbox == []


# --- ce qui ne doit JAMAIS déclencher l'alerte ---------------------------------------


async def test_no_alert_for_an_invited_member(
    client: AsyncClient,
    alert_outbox: list[dict],
    alert_on,
    system_roles: dict[str, Role],
) -> None:
    """L'alerte est liée à la naissance d'une AGENCE, pas d'un agent."""
    created = await _signup(client)
    assert created.status_code == 200
    before = len(alert_outbox)
    headers = {"Authorization": f"Bearer {created.json()['access_token']}"}

    invited = await client.post(
        "/agencies/me/invitations",
        headers=headers,
        json={"email": "collegue@agence.io", "role_id": str(system_roles["member"].id)},
    )
    assert invited.status_code == 201, invited.text
    assert len(alert_outbox) == before


async def test_no_alert_for_a_superadmin_created_agency(
    client: AsyncClient,
    alert_outbox: list[dict],
    alert_on,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    """Le wizard superadmin crée une agence que l'équipe connaît déjà : ce
    n'est pas une acquisition à signaler."""
    superadmin = await make_agent(role=system_roles["superadmin"])
    response = await client.post(
        "/agencies",
        headers=agent_headers(superadmin),
        json={
            "name": "Agence Interne",
            "admin_email": "wizard@exemple.io",
            "admin_first_name": "Wiz",
            "admin_last_name": "Ard",
            "default_language": "fr",
            "sectors": ["legal"],
        },
    )
    assert response.status_code == 201, response.text
    assert alert_outbox == []


# --- best-effort : l'inscription passe avant le mail ----------------------------------


async def test_a_send_failure_never_costs_the_signup(
    client: AsyncClient,
    db_session: AsyncSession,
    alert_on,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*a, **kw):
        raise RuntimeError("Resend est tombé")

    monkeypatch.setattr("src.signup.signup_alert.send_email", boom)

    response = await _signup(client)
    assert response.status_code == 200, response.text
    assert response.json()["access_token"]  # l'auto-login tient

    agency = (await db_session.execute(select(Agency))).scalars().one()
    assert agency.name == "Neo Agence"
    # Drapeau NON posé : rien n'est parti, la doctrine du lot onboarding.
    assert agency.signup_alert_sent_at is None


# --- la source : constat gravé --------------------------------------------------------


async def test_source_is_said_unknown_when_nothing_was_captured(
    client: AsyncClient, alert_outbox: list[dict], alert_on
) -> None:
    """Le signup ne capture aujourd'hui NI utm NI referrer. Le mail le DIT au
    lieu d'inventer — une source fausse est pire qu'une source absente."""
    assert (await _signup(client)).status_code == 200
    assert "Source     : inconnue" in alert_outbox[0]["body"]


async def test_source_names_the_sponsor_when_the_signup_was_referred(
    client: AsyncClient,
    db_session: AsyncSession,
    alert_outbox: list[dict],
    alert_on,
    make_agent: MakeAgent,
) -> None:
    """Le SEUL signal d'acquisition réellement capturé aujourd'hui : le code
    de parrainage du formulaire."""
    sponsor_agent = await make_agent()
    sponsor = (
        (await db_session.execute(select(Agency).where(Agency.id == sponsor_agent.agency_id)))
        .scalars()
        .one()
    )
    sponsor.referral_code = "PARRAIN1"
    await db_session.commit()

    assert (await _signup(client, referral_code="PARRAIN1")).status_code == 200
    body = alert_outbox[0]["body"]
    assert "parrainage par" in body and sponsor.slug in body


# --- les deux interrupteurs sont INDÉPENDANTS -----------------------------------------


async def test_muting_the_onboarding_mail_does_not_mute_the_alert(
    client: AsyncClient, alert_outbox: list[dict], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Le couplage d'origine : couper le mail de bienvenue de l'agence rendait
    l'équipe AVEUGLE aux inscriptions. Deux décisions sans rapport ne doivent
    pas tenir au même levier."""
    settings = get_settings()
    monkeypatch.setattr(settings, "onboarding_email_enabled", False)  # onboarding COUPÉ
    monkeypatch.setattr(settings, "signup_alert_enabled", True)  # alerte ARMÉE
    monkeypatch.setattr(settings, "signup_alert_recipients", list(RECIPIENTS))

    assert (await _signup(client)).status_code == 200
    assert sorted(m["to"] for m in alert_outbox) == sorted(RECIPIENTS)


async def test_muting_the_alert_does_not_depend_on_the_onboarding_switch(
    client: AsyncClient, alert_outbox: list[dict], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Et réciproquement : couper l'alerte n'emprunte rien à l'onboarding."""
    settings = get_settings()
    monkeypatch.setattr(settings, "onboarding_email_enabled", True)  # onboarding ARMÉ
    monkeypatch.setattr(settings, "signup_alert_enabled", False)  # alerte COUPÉE
    monkeypatch.setattr(settings, "signup_alert_recipients", list(RECIPIENTS))

    assert (await _signup(client)).status_code == 200
    assert alert_outbox == []


async def test_production_default_is_on_for_both_without_any_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ce que la prod fait AUJOURD'HUI : aucune des deux variables n'est posée
    sur Fly, donc les deux dérivent de l'environnement et valent true."""
    from src.agencies.onboarding_email_job import mail_enabled
    from src.signup.signup_alert import alert_enabled

    settings = get_settings()
    monkeypatch.setattr(settings, "onboarding_email_enabled", None)
    monkeypatch.setattr(settings, "signup_alert_enabled", None)
    monkeypatch.setattr(settings, "environment", "production")
    assert alert_enabled(settings) is True
    assert mail_enabled(settings) is True
    monkeypatch.setattr(settings, "environment", "development")
    assert alert_enabled(settings) is False
    assert mail_enabled(settings) is False

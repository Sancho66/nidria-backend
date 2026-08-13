"""Le mail d'onboarding à J+10 min (spec Eric 13/08, P1).

Couvre : (a) un seul envoi, au créateur de l'agence — jamais aux agents
invités ensuite, jamais à un prestataire ; (b) la survie au redémarrage
(l'état vit en base, pas dans la mémoire du scheduler) ; (c) les deux
gardes d'exclusion (l'interrupteur d'environnement, le motif d'adresse
interne) ; (d) le repli du prénom (« Bonjour, », jamais « Bonjour , ») ;
(e) les 4 langues ; (f) la phrase du dossier d'exemple servie SEULEMENT
s'il existe vraiment ; (g) le parc antérieur jamais balayé ; (h) un envoi
qui échoue rejoué au balayage suivant."""

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session, sessionmaker

from shared.models.agency import Agency
from shared.models.agent import Agent
from shared.models.rbac import Role
from src.agencies.onboarding_email_job import send_onboarding_emails
from src.core import email
from src.core.config import get_settings
from src.core.scheduler import JOB_REGISTRY
from src.jobs.jobs_baseline import DEFAULT_JOB_CONFIGS
from tests.plugins.agency_plugin import MakeAgency
from tests.plugins.agent_plugin import MakeAgent
from tests.plugins.case_plugin import MakeClientCase

pytestmark = pytest.mark.usefixtures("rbac_baseline")

NewAgency = Callable[..., Awaitable[tuple[Agency, Agent]]]


@pytest.fixture(autouse=True)
def enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """The env guard is ON by default in test (it derives from a
    non-production environment) — each test that expects a send turns it on
    explicitly, and one test proves the default silence."""
    monkeypatch.setattr(get_settings(), "onboarding_email_enabled", True)


@pytest.fixture
def new_agency(
    db_session: AsyncSession,
    make_agency: MakeAgency,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
) -> NewAgency:
    """An agency created `minutes_ago` ago with its FIRST admin — the shape
    both creation paths leave behind (wizard and self-signup)."""

    async def _make(
        *,
        minutes_ago: float = 11,
        first_name: str = "Sidney",
        email_address: str | None = None,
        lang: str = "fr",
        **agency_overrides: Any,
    ) -> tuple[Agency, Agent]:
        created_at = datetime.now(UTC) - timedelta(minutes=minutes_ago)
        agency = await make_agency(
            slug=f"onb-{uuid.uuid4().hex[:8]}",
            created_at=created_at,
            default_language=lang,
            **agency_overrides,
        )
        admin = await make_agent(
            role=system_roles["admin"],
            agency_id=agency.id,
            first_name=first_name,
            created_at=created_at,
            **({"email": email_address} if email_address else {}),
        )
        return agency, admin

    return _make


def _run(
    sync_session_local: sessionmaker[Session], *, dry_run: bool = False
) -> tuple[dict, list[str]]:
    lines: list[str] = []
    with sync_session_local() as db:
        stats = send_onboarding_emails(db, log=lines.append, dry_run=dry_run)
    return stats, lines


async def _sent_at(db: AsyncSession, agency_id: uuid.UUID) -> datetime | None:
    # Column select (never the entity): no identity-map cache to expire, so
    # the other objects of the test stay loaded.
    return (
        await db.execute(select(Agency.onboarding_email_sent_at).where(Agency.id == agency_id))
    ).scalar_one()


# --- (a) un seul envoi, au créateur ---------------------------------------------------


async def test_one_mail_to_the_creator_never_to_the_invited(
    db_session: AsyncSession,
    sync_session_local: sessionmaker[Session],
    new_agency: NewAgency,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
) -> None:
    agency, creator = await new_agency()
    # Trois personnes arrivées APRÈS : un membre, un lecteur, un prestataire.
    for role, is_external in (("member", False), ("viewer", False), ("member", True)):
        await make_agent(
            role=system_roles[role],
            agency_id=agency.id,
            is_external=is_external,
            created_at=datetime.now(UTC),
        )

    stats, _ = _run(sync_session_local)
    assert stats["sent"] == 1
    assert len(email.outbox) == 1
    assert email.outbox[0].to == creator.email  # le créateur, lui seul
    # Le mécanisme transactionnel existant : même expéditeur que l'activation
    # client (aucun override de sender comme le fait la nurture).
    assert email.outbox[0].sender is None
    assert await _sent_at(db_session, agency.id) is not None

    # Idempotence : le balayage suivant ne renvoie rien.
    stats, _ = _run(sync_session_local)
    assert stats["sent"] == 0
    assert len(email.outbox) == 1


# --- (b) la survie au redémarrage -----------------------------------------------------


async def test_the_trigger_survives_a_restart(
    db_session: AsyncSession, sync_session_local: sessionmaker[Session], new_agency: NewAgency
) -> None:
    """Rien n'est programmé en mémoire : une agence trop jeune est ignorée,
    et le SEUL fait qu'elle vieillisse suffit à déclencher l'envoi au
    balayage suivant — un redémarrage entre les deux ne change rien."""
    agency, _ = await new_agency(minutes_ago=2)
    stats, _ = _run(sync_session_local)
    assert stats == {
        "due": 0,
        "sent": 0,
        "excluded": 0,
        "no_recipient": 0,
        "failed": 0,
    }
    assert email.outbox == []

    # Le temps passe (et le process a redémarré entre-temps : aucun état
    # n'était en mémoire de toute façon).
    agency.created_at = datetime.now(UTC) - timedelta(minutes=11)
    await db_session.commit()

    stats, _ = _run(sync_session_local)
    assert stats["sent"] == 1
    assert await _sent_at(db_session, agency.id) is not None


# --- (c) les deux gardes d'exclusion --------------------------------------------------


async def test_the_environment_switch_silences_everything(
    db_session: AsyncSession,
    sync_session_local: sessionmaker[Session],
    new_agency: NewAgency,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Un seed local de 20 agences ne doit pas produire 20 emails : hors
    production, l'interrupteur est fermé par défaut (None → environment)."""
    monkeypatch.setattr(get_settings(), "onboarding_email_enabled", None)
    agency, _ = await new_agency()
    stats, lines = _run(sync_session_local)
    assert stats["disabled"] is True
    assert email.outbox == []
    assert await _sent_at(db_session, agency.id) is None  # rien de brûlé
    assert any("disabled" in line for line in lines)


async def test_internal_addresses_are_excluded_by_config(
    db_session: AsyncSession,
    sync_session_local: sessionmaker[Session],
    new_agency: NewAgency,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        get_settings(), "onboarding_email_excluded_patterns", ["@nidria.com", "+test@"]
    )
    interne, _ = await new_agency(email_address="eric@nidria.com")
    alias, _ = await new_agency(email_address="alex+test@gmail.com")
    cliente, _ = await new_agency(email_address="nicolas@domiciliation-bulgarie.fr")

    stats, _ = _run(sync_session_local)
    assert stats["excluded"] == 2
    assert [mail.to for mail in email.outbox] == ["nicolas@domiciliation-bulgarie.fr"]
    # Un exclu n'est jamais estampillé : le drapeau veut dire « envoyé ».
    assert await _sent_at(db_session, interne.id) is None
    assert await _sent_at(db_session, alias.id) is None
    assert await _sent_at(db_session, cliente.id) is not None


# --- (d) le repli du prénom -----------------------------------------------------------


async def test_the_first_name_falls_back_without_a_hole(
    sync_session_local: sessionmaker[Session], new_agency: NewAgency
) -> None:
    """Le champ est NOT NULL mais `min_length=1` laisse passer un blanc :
    « Bonjour, » — jamais « Bonjour , », jamais un trou."""
    await new_agency(first_name="   ")
    _run(sync_session_local)
    text = email.outbox[0].body
    assert "Bonjour, votre espace Nidria est prêt." in text
    assert "Bonjour ," not in text
    assert "Bonjour  " not in text


# --- (e) les 4 langues ----------------------------------------------------------------


async def test_the_four_languages_and_the_french_video(
    sync_session_local: sessionmaker[Session], new_agency: NewAgency
) -> None:
    for lang in ("fr", "en", "es", "hu"):
        await new_agency(lang=lang, first_name=f"Admin{lang}")
    _run(sync_session_local)

    by_subject = {mail.subject for mail in email.outbox}
    assert by_subject == {
        "Bienvenue dans Nidria",
        "Welcome to Nidria",
        "Bienvenido a Nidria",
        "Üdvözöljük a Nidriában",
    }
    video = get_settings().onboarding_video_url
    for mail in email.outbox:
        assert video in mail.body  # le bouton vidéo dans les 4
        assert get_settings().frontend_url in mail.body  # et le CTA vers l'espace
    # La vidéo reste FRANÇAISE (décision Eric) : les versions non-FR le disent.
    fr = next(m for m in email.outbox if m.subject == "Bienvenue dans Nidria")
    en = next(m for m in email.outbox if m.subject == "Welcome to Nidria")
    assert "francais" not in fr.body.lower() and "français" not in fr.body.lower()
    assert "The video is in French." in en.body


# --- (f) la promesse du dossier d'exemple ---------------------------------------------


async def test_the_example_sentence_only_when_the_example_exists(
    db_session: AsyncSession,
    sync_session_local: sessionmaker[Session],
    new_agency: NewAgency,
    make_client_case: MakeClientCase,
    make_expat_user: Any,
) -> None:
    """« un dossier d'exemple est déjà prêt » n'est envoyé que si c'est
    VRAI : une agence sans dossier de démo reçoit le mail sans la phrase."""
    avec, _ = await new_agency()
    expat = await make_expat_user()
    case = await make_client_case(agency_id=avec.id, principal_expat_user_id=expat.id)
    case.is_demo = True
    await db_session.commit()
    sans, _ = await new_agency()

    _run(sync_session_local)
    sent = {mail.to: mail.body for mail in email.outbox}
    admins = {
        agency_id: (
            await db_session.execute(select(Agent.email).where(Agent.agency_id == agency_id))
        ).scalar_one()
        for agency_id in (avec.id, sans.id)
    }
    assert "dossier d'exemple est déjà prêt" in sent[admins[avec.id]]
    assert "dossier d'exemple" not in sent[admins[sans.id]]


# --- (g) le parc antérieur ------------------------------------------------------------


async def test_the_existing_park_is_never_swept(
    db_session: AsyncSession, sync_session_local: sessionmaker[Session], new_agency: NewAgency
) -> None:
    """Aucun backfill de migration : une agence créée avant la feature est
    hors fenêtre de rattrapage, donc jamais « souhaitée bienvenue » des mois
    après — et son drapeau reste NULL, sans fausse date d'envoi."""
    vieille, _ = await new_agency(minutes_ago=60 * 24 * 3)  # 3 jours
    stats, _ = _run(sync_session_local)
    assert stats["due"] == 0
    assert email.outbox == []
    assert await _sent_at(db_session, vieille.id) is None


async def test_internal_agencies_are_out_of_scope(
    sync_session_local: sessionmaker[Session], new_agency: NewAgency
) -> None:
    await new_agency(is_internal=True)
    assert _run(sync_session_local)[0]["due"] == 0
    assert email.outbox == []


# --- (h) un envoi qui échoue est rejoué ------------------------------------------------


async def test_a_failed_send_is_retried_at_the_next_sweep(
    db_session: AsyncSession,
    sync_session_local: sessionmaker[Session],
    new_agency: NewAgency,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Le drapeau est posé APRÈS l'envoi : un échec le laisse NULL, et le
    balayage suivant rejoue."""
    agency, admin = await new_agency()
    import src.agencies.onboarding_email_job as job

    def _boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("resend down")

    monkeypatch.setattr(job, "send_email", _boom)
    stats, lines = _run(sync_session_local)
    assert stats["failed"] == 1 and stats["sent"] == 0
    assert await _sent_at(db_session, agency.id) is None
    assert any("FAILED" in line for line in lines)

    # Targeted restore — `monkeypatch.undo()` would also revert the autouse
    # `enabled` fixture (same monkeypatch instance) and silence the job.
    monkeypatch.setattr(job, "send_email", email.send_email)
    stats, _ = _run(sync_session_local)
    assert stats["sent"] == 1
    assert email.outbox[0].to == admin.email
    assert await _sent_at(db_session, agency.id) is not None


# --- le câblage -------------------------------------------------------------------------


def test_the_job_is_registered_and_seeded() -> None:
    """Un pipeline sans config (ou l'inverse) ne tournerait jamais."""
    assert "onboarding_email" in JOB_REGISTRY
    seeded = {spec["job_id"]: spec for spec in DEFAULT_JOB_CONFIGS}
    assert seeded["onboarding_email"]["cron_expression"] == "*/5 * * * *"


async def test_dry_run_sends_nothing_and_stamps_nothing(
    db_session: AsyncSession, sync_session_local: sessionmaker[Session], new_agency: NewAgency
) -> None:
    agency, _ = await new_agency()
    stats, lines = _run(sync_session_local, dry_run=True)
    assert stats["sent"] == 1 and stats["dry_run"] is True
    assert email.outbox == []
    assert await _sent_at(db_session, agency.id) is None
    assert any("would send" in line for line in lines)

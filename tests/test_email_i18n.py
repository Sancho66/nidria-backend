"""Email templates i18n — six languages, strict parity (Eric's audit).

Two bugs fixed here: (1) the auto follow-up body was hardcoded ENGLISH — it now
reaches the client in THEIR language; (2) the agent templates (password reset,
agent invitation) were hardcoded FRENCH and Italian fell back to French — now
translated. A parity test guards every catalog so a missing translation fails
CI, not the client. A MANUAL reminder body stays the agency's exact text
(never auto-translated — a legal decision, not ours)."""

import html as html_lib
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session, sessionmaker

import src.core.email_templates as et
from shared.models.agent import Agent
from shared.models.case_step_progress import CaseStepProgress
from shared.models.client_case import ClientCase
from shared.models.rbac import Role
from shared.models.reminder import Reminder
from src.core.email_templates import (
    agent_invitation_email,
    auto_reminder_body,
    password_reset_email,
    reminder_email,
    reminder_escalation_email,
)
from src.core.i18n import SUPPORTED_LANGUAGES
from src.reminders.reminders_jobs import create_auto_reminders
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent
from tests.plugins.case_plugin import MakeClientCase
from tests.plugins.expat_plugin import MakeExpatUser


@pytest.fixture
def i18n_client(client: AsyncClient, rbac_baseline: None) -> AsyncClient:
    return client


@pytest_asyncio.fixture
async def admin(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    return await make_agent(role=system_roles["admin"])


# --- parity: no catalog silently falls back to French --------------------------------


def test_every_email_catalog_covers_all_six_languages() -> None:
    """Introspects the email_templates module: EVERY per-language catalog (a
    module dict keyed by language, detected by its 'fr' key) must carry ALL
    SUPPORTED_LANGUAGES. A new catalog, or a missing translation (Italian or
    any other), fails here — the feasible, low-cost parity check for emails."""
    catalogs = {
        name: value for name, value in vars(et).items() if isinstance(value, dict) and "fr" in value
    }
    assert len(catalogs) >= 12, sorted(catalogs)  # the introspection found them all
    missing = {
        name: sorted(set(SUPPORTED_LANGUAGES) - set(cat))
        for name, cat in catalogs.items()
        if set(cat) != set(SUPPORTED_LANGUAGES)
    }
    assert not missing, f"email catalogs missing languages: {missing}"


# --- BUG 2: agent templates translated, Italian served like the other five -----------


def test_agent_invitation_in_italian() -> None:
    content = agent_invitation_email("Studio Rossi", "https://x/accept", 7, "it")
    assert "sei invitato" in content.subject.lower()  # genuinely Italian
    assert "Studio Rossi" in content.subject
    assert "giorni" in content.text  # the validity line is Italian too


def test_password_reset_in_italian() -> None:
    content = password_reset_email("https://x/reset", 60, "it")
    assert "reimposta la password" in content.subject.lower()
    assert "minuti" in content.text  # 60 min → minutes form, Italian


# --- feature NOT done: a manual reminder body is passed through, never translated -----


def test_manual_reminder_body_is_passed_through_untranslated() -> None:
    """Non-regression: the agency's free text ships verbatim. Auto-translating
    it is a legal decision (Eric's), not ours."""
    body = "Votre dossier a été refusé. Contactez-nous avant vendredi."
    content = reminder_email("Agence Durand", body, None, "es")  # Spanish CHROME…
    assert body in content.text  # …but the agency body is EXACTLY as written
    assert html_lib.escape(body) in content.html
    assert "Recordatorio" in content.text  # the chrome is Spanish (title)


# --- escalation prefix now exists in all six; the wrapped body stays untouched --------


def test_escalation_prefix_translated_and_wraps_original() -> None:
    content = reminder_escalation_email("Agence", "Me Rossi", "Fournir l'acte.", "it")
    assert "irraggiungibile" in content.text.lower()  # Italian escalation prefix
    assert "Fournir l'acte." in content.text  # original body wrapped, unchanged


# --- BUG 1: the auto follow-up body ships in the RECIPIENT's language -----------------


def _run_auto(session_local: sessionmaker[Session]) -> dict:
    with session_local() as db:
        return create_auto_reminders(db, log=lambda _: None)


async def _stalled_case(
    client: AsyncClient,
    db: AsyncSession,
    admin: Agent,
    make_client_case: MakeClientCase,
    principal_id: object,
    headers: dict,
) -> ClientCase:
    template = (await client.post("/journeys", headers=headers, json={"name": "T"})).json()
    await client.post(
        f"/journeys/{template['id']}/steps", headers=headers, json={"name": "Etape figee"}
    )
    case = await make_client_case(agency_id=admin.agency_id, principal_expat_user_id=principal_id)
    await client.post(
        f"/cases/{case.id}/journey", headers=headers, json={"journey_template_id": template["id"]}
    )
    await db.execute(
        update(CaseStepProgress)
        .where(CaseStepProgress.case_id == case.id)
        .values(updated_at=datetime.now(UTC) - timedelta(days=21))
    )
    await db.commit()
    return case


@pytest.mark.parametrize("lang", ["fr", "en", "es", "ru", "pt", "it"])
async def test_auto_reminder_body_in_recipient_language(
    i18n_client: AsyncClient,
    db_session: AsyncSession,
    sync_session_local: sessionmaker[Session],
    admin: Agent,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
    agent_headers: AuthHeaders,
    lang: str,
) -> None:
    """An auto follow-up to an expat whose preferred_lang is `lang` is stored
    with a body in THAT language — es → Spanish, it → Italian, etc."""
    headers = agent_headers(admin)
    expat = await make_expat_user(preferred_lang=lang, email=f"client-{lang}@x.io")
    case = await _stalled_case(i18n_client, db_session, admin, make_client_case, expat.id, headers)

    assert _run_auto(sync_session_local)["created"] == 1

    reminder = (
        await db_session.execute(select(Reminder).where(Reminder.case_id == case.id))
    ).scalar_one()
    # The stored body is the translated template for the recipient's language
    # (threshold 20 = the first J+N band), never the old hardcoded English.
    assert reminder.message_body == auto_reminder_body("Etape figee", 20, lang)
    if lang != "en":
        assert "Automatic follow-up" not in reminder.message_body  # the ex-bug is gone

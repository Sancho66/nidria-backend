"""ICP multi-métier (Eric): the client case-invitation emails carry the
JOURNEY NAME resolved in the recipient language, with a neutral fallback,
and NEVER the hardcoded "expatriation" of the past.

Covers: (a) the invitation names the journey, in the recipient language;
(b) a case with no journey falls back to the neutral "your case"; (c) no
business-specific term ("expatriation/expat/relocation/immigration") is
hardcoded anywhere in the email templates, in any of the 6 languages;
plus the real end-to-end path (a created case's invite email carries the
journey name)."""

import re

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

import src.core.email_templates as et
from shared.models.agent import Agent
from shared.models.rbac import Role
from src.core import email
from src.core.email_templates import expat_activation_email, new_case_email
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent

pytestmark = pytest.mark.usefixtures("rbac_baseline")

# Business-specific terms the multi-métier ICP forbids in the chrome (the
# journey NAME is agency content and is the ONLY place a métier surfaces).
_FORBIDDEN = re.compile(r"expatri|\bexpat\b|relocation|reubicaci|переезд", re.IGNORECASE)


@pytest_asyncio.fixture
async def admin(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    return await make_agent(role=system_roles["admin"])


# --- (a) the journey name is carried, in the recipient language ----------------------


def test_invitation_carries_the_journey_name_per_language() -> None:
    # Activation invite, RU recipient, RU journey name variant.
    c = expat_activation_email(
        "Acme", "https://x/space", 14, journey_name="Создание компании", lang="ru"
    )
    assert "Создание компании" in c.text
    assert 'html lang="ru"' in c.html
    assert not _FORBIDDEN.search(c.text)

    # "A new case awaits you", EN.
    n = new_case_email("Acme", "https://x/login", journey_name="Company setup", lang="en")
    assert "Company setup" in n.text
    assert 'html lang="en"' in n.html
    assert not _FORBIDDEN.search(n.text)


# --- (b) no journey -> neutral fallback, never a business term ------------------------


def test_no_journey_falls_back_to_neutral_case() -> None:
    c = expat_activation_email("Acme", "https://x/space", 14, journey_name=None, lang="fr")
    assert "votre dossier" in c.text
    assert "«" not in c.text  # no empty quotes when there is no name
    assert not _FORBIDDEN.search(c.text)

    n = new_case_email("Acme", "https://x/login", journey_name=None, lang="es")
    assert "su expediente" in n.text
    assert not _FORBIDDEN.search(n.text)


# --- (c) no business term hardcoded in ANY template, ANY language ---------------------


def test_no_business_term_hardcoded_in_any_catalog() -> None:
    """Grep every 6-language catalog string in the module: subjects,
    titles, intros, buttons must be métier-neutral. The journey name is
    injected at runtime and is the only métier-bearing text."""
    catalogs = [
        v
        for name, v in vars(et).items()
        if name.isupper() and isinstance(v, dict) and name != "_DOSSIER"
    ]
    assert len(catalogs) >= 5  # reminder, requirement, reopened, ready, activation, new_case...
    offenders: list[str] = []
    for catalog in catalogs:
        for lang_block in catalog.values():
            values = lang_block.values() if isinstance(lang_block, dict) else [lang_block]
            for text in values:
                if isinstance(text, str) and _FORBIDDEN.search(text):
                    offenders.append(text)
    assert offenders == [], offenders

    # And the two invitation builders themselves, rendered in all 6 langs
    # with a neutral journey, carry no forbidden term.
    for lang in ("fr", "en", "es", "ru", "pt", "it"):
        for content in (
            expat_activation_email("A", "u", 14, None, lang),
            new_case_email("A", "u", None, lang),
        ):
            assert not _FORBIDDEN.search(content.subject + content.text), lang


# --- end-to-end: a real created case's invite carries the journey name ---------------


@pytest_asyncio.fixture(autouse=True)
def _capture_emails(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    sent: list[dict] = []

    def _fake_send(to: str, subject: str, text: str, html: str) -> None:
        sent.append({"to": to, "subject": subject, "text": text, "html": html})

    monkeypatch.setattr(email, "send_email", _fake_send)
    monkeypatch.setattr("src.cases.cases_manager.send_email", _fake_send)
    return sent


async def test_created_case_invite_email_names_the_journey(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    _capture_emails: list[dict],
) -> None:
    headers = agent_headers(admin)
    tid = (
        await client.post("/journeys", headers=headers, json={"name": "Création de société"})
    ).json()["id"]
    created = await client.post(
        "/cases",
        headers=headers,
        json={
            "first_name": "Marie",
            "last_name": "Curie",
            "email": "marie@example.com",
            "journey_template_id": tid,
        },
    )
    assert created.status_code == 201, created.text

    invite = next(m for m in _capture_emails if m["to"] == "marie@example.com")
    assert "Création de société" in invite["text"]
    assert not _FORBIDDEN.search(invite["text"])

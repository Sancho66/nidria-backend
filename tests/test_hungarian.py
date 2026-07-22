"""Le hongrois, 7e langue (décision Eric 2026-07-20) : la liste centrale
dérive tout — la parité x7 des gabarits, le signup en hu, le catalogue de
champs en hu, la traduction IA qui accepte hu en cible."""

import ast
import uuid
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from shared.models.journey import JourneyTemplate, JourneyTemplateStep
from shared.models.rbac import Role
from src.core import email
from src.core.i18n import SUPPORTED_LANGUAGES
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent

pytestmark = pytest.mark.usefixtures("rbac_baseline")


@pytest_asyncio.fixture
async def admin(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    return await make_agent(role=system_roles["admin"])


def test_hu_is_supported_and_every_catalog_is_x7() -> None:
    """La parité x7 STRUCTURELLE : chaque catalogue de gabarit porte les 7
    langues — le fallback de _pick ne peut plus masquer un trou."""
    assert "hu" in SUPPORTED_LANGUAGES and len(SUPPORTED_LANGUAGES) == 7
    tree = ast.parse(Path("src/core/email_templates.py").read_text())
    checked = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Dict):
            try:
                d = ast.literal_eval(node.value)
            except (ValueError, SyntaxError):
                continue
            if isinstance(d, dict) and "fr" in d:
                name = getattr(node.targets[0], "id", "?")
                missing = set(SUPPORTED_LANGUAGES) - set(d)
                assert not missing, f"{name}: langues manquantes {missing}"
                if isinstance(d["fr"], dict):
                    assert set(d["hu"]) == set(d["fr"]), f"{name}: cles hu != fr"
                checked += 1
    assert checked >= 20  # les 21 types (footer/copy-paste compris)


def test_field_catalog_is_x7() -> None:
    tree = ast.parse(Path("src/journeys/field_catalog.py").read_text())
    blobs = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            try:
                d = ast.literal_eval(node)
            except (ValueError, SyntaxError):
                continue
            if isinstance(d, dict) and "fr" in d and "en" in d:
                assert "hu" in d, f"blob sans hu ligne {node.lineno}"
                if isinstance(d["fr"], list):
                    assert len(d["hu"]) == len(d["fr"])  # les options en parite
                blobs += 1
    # 101 libelles (69 + 32 des packs sectoriels) + 20 listes d'options (8 + 12).
    assert blobs == 121


async def test_signup_code_email_leaves_in_hungarian(client: AsyncClient) -> None:
    from src.core import ratelimit

    ratelimit.reset()
    email.outbox.clear()
    r = await client.post("/signup", json={"email": "magyar@example.com", "lang": "hu"})
    assert r.status_code == 200, r.text
    [sent] = email.outbox
    assert sent.subject == "Nidria: az Ön ellenőrző kódja"  # le hu, pas le fallback


async def test_agency_speaks_hungarian_and_zai_accepts_hu_target(
    client: AsyncClient,
    admin: Agent,
    agent_headers: AuthHeaders,
) -> None:
    """default_language=hu passe (Literal + CHECK SQL élargis), et
    l'estimation de traduction accepte hu en cible (la liste dérive)."""
    ah = agent_headers(admin)
    patched = await client.patch("/agencies/me", headers=ah, json={"default_language": "hu"})
    assert patched.status_code == 200, patched.text
    assert patched.json()["default_language"] == "hu"
    tid = (await client.post("/journeys", headers=ah, json={"name": "Ut"})).json()["id"]
    await client.post(f"/journeys/{tid}/steps", headers=ah, json={"name": "Lepes"})
    estimate = await client.get(f"/journeys/{tid}/translate/estimate?langs=fr", headers=ah)
    # l'agence est en hu : fr est une cible valide ; et hu en cible depuis
    # une agence fr passe pareil (la liste centrale, pas une liste en dur)
    assert estimate.status_code == 200, estimate.text


# --- the AI translation chain serves and writes hu (BUG hongrois, 2026-07-20) -----------------


@pytest.fixture
def hu_provider(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Echo provider at the raw-call boundary, one call per language lot."""
    state: dict[str, Any] = {"calls": []}

    async def _fake(
        items: list[dict[str, str]],
        source_lang: str,
        target_langs: list[str],
        strict_retry: bool = False,
    ):
        state["calls"].append(list(target_langs))
        translations = {
            item["key"]: {
                lang: f"[{lang}] {'Перевод ' if lang == 'ru' else ''}{item['text']}"
                for lang in target_langs
            }
            for item in items
        }
        return translations, {"prompt_tokens": 900, "completion_tokens": 2500}

    import src.journeys.translation_manager as tm

    monkeypatch.setattr(tm.translation_client, "request_translations", _fake)
    return state


async def test_editing_language_accepts_hu(
    client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    """Before the fix: the API validation (derived) said yes and the DB
    CHECK (stuck at 6) blew up in a 500. Both speak hu now."""
    ah = agent_headers(admin)
    tid = (await client.post("/journeys", headers=ah, json={"name": "Ut"})).json()["id"]
    patched = await client.patch(f"/journeys/{tid}", headers=ah, json={"editing_language": "hu"})
    assert patched.status_code == 200, patched.text
    detail = await client.get(f"/journeys/{tid}", headers=ah)
    assert detail.json()["editing_language"] == "hu"


async def test_translation_job_serves_and_writes_hu(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    hu_provider: dict[str, Any],
) -> None:
    """The list served to the front carries hu, and the job WRITES the
    hu content."""
    ah = agent_headers(admin)
    tid = (await client.post("/journeys", headers=ah, json={"name": "Résidence D7"})).json()["id"]
    step = await client.post(f"/journeys/{tid}/steps", headers=ah, json={"name": "Dépôt"})
    assert step.status_code == 201
    step_id = step.json()["id"]

    estimate = (await client.get(f"/journeys/{tid}/translate/estimate", headers=ah)).json()
    assert "hu" in estimate["langs"] and "hu" in estimate["counts"]

    started = await client.post(f"/journeys/{tid}/translate", headers=ah, json={})
    assert started.status_code == 202, started.text
    assert "hu" in started.json()["langs"]

    row = await db_session.get(JourneyTemplateStep, uuid.UUID(step_id))
    assert row is not None
    assert row.name_i18n["hu"] == "[hu] Dépôt"  # the hu content IS written


async def test_x6_template_retranslates_hu_alone(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    hu_provider: dict[str, Any],
) -> None:
    """A template already translated in the 6 historic languages: the
    fill-empty-only job proposes and consumes hu ALONE (one lot), the
    six existing variants untouched."""
    ah = agent_headers(admin)
    tid = (await client.post("/journeys", headers=ah, json={"name": "Résidence D7"})).json()["id"]
    template = await db_session.get(JourneyTemplate, uuid.UUID(tid))
    assert template is not None
    template.name_i18n = {
        "fr": "Résidence D7",
        **{lang: f"human {lang}" for lang in ("en", "es", "it", "pt", "ru")},
    }
    await db_session.commit()

    estimate = (await client.get(f"/journeys/{tid}/translate/estimate", headers=ah)).json()
    assert estimate["langs"] == ["hu"]  # the ONLY lot with work

    started = await client.post(f"/journeys/{tid}/translate", headers=ah, json={})
    assert started.status_code == 202, started.text
    assert started.json()["langs"] == ["hu"]
    assert hu_provider["calls"] == [["hu"]]  # one call, hu alone — the 6 not re-consumed

    db_session.expire_all()
    template = await db_session.get(JourneyTemplate, uuid.UUID(tid))
    assert template is not None
    assert template.name_i18n["hu"] == "[hu] Résidence D7"
    assert template.name_i18n["en"] == "human en"  # untouched

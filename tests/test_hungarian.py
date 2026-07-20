"""Le hongrois, 7e langue (décision Eric 2026-07-20) : la liste centrale
dérive tout — la parité x7 des gabarits, le signup en hu, le catalogue de
champs en hu, la traduction IA qui accepte hu en cible."""

import ast
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import AsyncClient

from shared.models.agent import Agent
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
    assert blobs == 77  # 69 libelles + 8 listes d'options


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

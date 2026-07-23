"""Signup self-serve, code par email — le premier write PUBLIC du produit.

Le code 6 chiffres (hashe, 15 min, MORT a la 5e tentative), le
completion_token court (30 min), la creation en UNE transaction par le
writer partage (trial, referral, demo, milestones identiques au wizard),
l'auto-login. Zero oracle : code faux == expire == mort (meme reponse) ;
le POST initial repond 200 que l'email existe ou non."""

from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agency import Agency
from shared.models.agent import Agent
from shared.models.rbac import Role
from shared.models.signup import SignupVerification
from src.core import ratelimit
from tests.plugins.agent_plugin import MakeAgent

pytestmark = pytest.mark.usefixtures("rbac_baseline")


@pytest.fixture(autouse=True)
def _fixed_code_and_captured_mail(monkeypatch: pytest.MonkeyPatch):
    """Code deterministe + capture des envois (zero reseau)."""
    monkeypatch.setattr("src.signup.signup_manager._generate_code", lambda: "123456")
    sent: list[dict] = []
    monkeypatch.setattr(
        "src.signup.signup_manager.send_email",
        lambda to, subject, text, html=None, **kw: sent.append(
            {"to": to, "subject": subject, "text": text}
        ),
    )
    ratelimit.reset()
    yield sent
    ratelimit.reset()


@pytest.fixture
def sent(_fixed_code_and_captured_mail) -> list[dict]:
    return _fixed_code_and_captured_mail


async def _request(client: AsyncClient, email: str = "neo@agence.io", **extra):
    return await client.post("/signup", json={"email": email, "lang": "fr", **extra})


async def _verify(client: AsyncClient, email: str = "neo@agence.io", code: str = "123456"):
    return await client.post("/signup/verify", json={"email": email, "code": code})


async def _complete(client: AsyncClient, token: str, **overrides):
    payload = {
        "completion_token": token,
        "agency_name": "Neo Agence",
        "first_name": "Neo",
        "last_name": "Fondateur",
        "password": "MotDePasse1!",
        "language": "fr",
        "sectors": ["legal"],  # chosen in the form (mandatory)
    }
    payload.update(overrides)
    if payload.get("sectors") is None:
        payload.pop("sectors", None)  # None → omit the key entirely
    return await client.post("/signup/complete", json=payload)


# --- le flux nominal, de bout en bout ---------------------------------------------------


async def test_full_flow_creates_everything_and_logs_in(
    client: AsyncClient, db_session: AsyncSession, sent: list[dict]
) -> None:
    assert (await _request(client)).status_code == 200
    assert sent[-1]["to"] == "neo@agence.io" and "123456" in sent[-1]["text"]

    verified = await _verify(client)
    assert verified.status_code == 200, verified.text
    token = verified.json()["completion_token"]
    assert len(token) >= 16

    done = await _complete(client, token)
    assert done.status_code == 200, done.text
    pair = done.json()
    assert pair["access_token"] and pair["refresh_token"]

    # TOUT est la, comme au wizard : agence en essai 30 j, code de
    # parrainage propre, admin au bon mot de passe, dossier demo.
    agency = (
        await db_session.execute(select(Agency).where(Agency.name == "Neo Agence"))
    ).scalar_one()
    assert agency.slug == "neo-agence"
    assert agency.trial_ends_at is not None and agency.referral_code.startswith("NID-")
    # Self-signup defers the sector choice: born with [] AND flagged so the
    # front shows the blocking sector-onboarding screen. THIS is the only
    # path that poses the flag true.
    assert agency.sectors == ["legal"]  # chosen in the form, written atomically
    assert agency.sectors_onboarding_required is False  # no post-signup wall
    # NID-16a: signup poses a currency too (fr → EUR) — never NULL, so a
    # fresh self-serve agency never hits the cost.currency_required wall.
    assert agency.currency == "EUR"
    admin = (
        await db_session.execute(select(Agent).where(Agent.email == "neo@agence.io"))
    ).scalar_one()
    assert admin.agency_id == agency.id
    # L'auto-login est REEL : le token pair fonctionne.
    me = await client.get(
        "/auth/agent/me", headers={"Authorization": f"Bearer {pair['access_token']}"}
    )
    assert me.status_code == 200 and me.json()["email"] == "neo@agence.io"
    # La verification est consommee : plus de ligne.
    left = (
        (
            await db_session.execute(
                select(SignupVerification).where(SignupVerification.email == "neo@agence.io")
            )
        )
        .scalars()
        .all()
    )
    assert left == []


async def test_signup_with_referral_code_attributes(
    client: AsyncClient, db_session: AsyncSession, make_agency, sent: list[dict]
) -> None:
    referrer = await make_agency(slug="marraine-signup")
    await db_session.execute(
        update(Agency).where(Agency.id == referrer.id).values(referral_code="NID-MARRA1")
    )
    await db_session.commit()
    await _request(client)
    token = (await _verify(client)).json()["completion_token"]
    done = await _complete(client, token, referral_code="nid-marra1")
    assert done.status_code == 200, done.text
    agency = (
        await db_session.execute(select(Agency).where(Agency.name == "Neo Agence"))
    ).scalar_one()
    assert agency.referred_by_agency_id == referrer.id


async def test_slug_collision_gets_a_suffix(
    client: AsyncClient, db_session: AsyncSession, make_agency, sent: list[dict]
) -> None:
    await make_agency(slug="neo-agence")
    await _request(client)
    token = (await _verify(client)).json()["completion_token"]
    assert (await _complete(client, token)).status_code == 200
    agency = (
        await db_session.execute(select(Agency).where(Agency.name == "Neo Agence"))
    ).scalar_one()
    assert agency.slug == "neo-agence-2"


# --- zero oracle, zero fantome ----------------------------------------------------------


async def test_known_email_gets_the_other_email_same_response(
    client: AsyncClient,
    db_session: AsyncSession,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    sent: list[dict],
) -> None:
    await make_agent(role=system_roles["admin"], email="deja@la.io")
    resp = await _request(client, email="deja@la.io")
    unknown = await _request(client, email="jamais@vu.io")
    # HTTP identique (pas d'enumeration) — le CONTENU du mail differe.
    assert resp.status_code == unknown.status_code == 200
    assert resp.json() == unknown.json()
    bodies = {m["to"]: m["subject"] for m in sent}
    assert "compte" in bodies["deja@la.io"].lower()  # "vous avez deja un compte"
    assert "code" in bodies["jamais@vu.io"].lower()
    # Et RIEN n'est cree pour l'email connu.
    rows = (
        (
            await db_session.execute(
                select(SignupVerification).where(SignupVerification.email == "deja@la.io")
            )
        )
        .scalars()
        .all()
    )
    assert rows == []


async def test_wrong_expired_and_dead_codes_share_one_answer(
    client: AsyncClient, db_session: AsyncSession, sent: list[dict]
) -> None:
    await _request(client)
    wrong = await _verify(client, code="000000")
    assert wrong.status_code == 400
    body_wrong = wrong.json()

    # Expire : meme reponse exactement.
    await db_session.execute(
        update(SignupVerification)
        .where(SignupVerification.email == "neo@agence.io")
        .values(expires_at=datetime.now(UTC) - timedelta(minutes=1))
    )
    await db_session.commit()
    expired = await _verify(client)  # le BON code, mais expire
    assert expired.status_code == 400 and expired.json() == body_wrong

    # Inconnu : pareil.
    unknown = await _verify(client, email="fantome@x.io")
    assert unknown.status_code == 400 and unknown.json() == body_wrong


async def test_brute_force_kills_the_code_at_the_fifth_attempt(
    client: AsyncClient, sent: list[dict]
) -> None:
    await _request(client)
    for _ in range(5):
        assert (await _verify(client, code="999999")).status_code == 400
    # 6e tentative avec le BON code : le code est MORT, meme reponse.
    dead = await _verify(client)
    assert dead.status_code == 400
    assert dead.json()["code"] == "signup.invalid_code"


async def test_new_request_kills_the_previous_code(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, sent: list[dict]
) -> None:
    await _request(client)
    # Nouveau code (different) : l'ancien meurt.
    monkeypatch.setattr("src.signup.signup_manager._generate_code", lambda: "654321")
    await _request(client)
    rows = (
        (
            await db_session.execute(
                select(SignupVerification).where(SignupVerification.email == "neo@agence.io")
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1  # UNE verification vivante par email
    assert (await _verify(client, code="123456")).status_code == 400  # l'ancien
    assert (await _verify(client, code="654321")).status_code == 200  # le vivant


async def test_completion_token_forged_or_expired_refused(
    client: AsyncClient, db_session: AsyncSession, sent: list[dict]
) -> None:
    forged = await _complete(client, "A" * 43)
    assert forged.status_code == 400
    assert forged.json()["code"] == "signup.invalid_token"

    await _request(client)
    token = (await _verify(client)).json()["completion_token"]
    await db_session.execute(
        update(SignupVerification)
        .where(SignupVerification.completion_token == token)
        .values(completion_expires_at=datetime.now(UTC) - timedelta(minutes=1))
    )
    await db_session.commit()
    expired = await _complete(client, token)
    assert expired.status_code == 400 and expired.json()["code"] == "signup.invalid_token"


async def test_honeypot_answers_200_and_creates_nothing(
    client: AsyncClient, db_session: AsyncSession, sent: list[dict]
) -> None:
    resp = await _request(client, website="https://spam.example")
    assert resp.status_code == 200  # le bot ne sait pas qu'il est vu
    assert sent == []  # aucun mail
    rows = (await db_session.execute(select(SignupVerification))).scalars().all()
    assert rows == []


async def test_rate_limit_answers_429(client: AsyncClient, sent: list[dict]) -> None:
    for i in range(10):
        assert (await _request(client, email=f"n{i}@x.io")).status_code == 200
    blocked = await _request(client, email="n11@x.io")
    assert blocked.status_code == 429


async def test_turnstile_flag_armed_requires_a_token(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, sent: list[dict]
) -> None:
    from src.core.config import get_settings

    monkeypatch.setenv("TURNSTILE_SECRET", "test-secret")
    get_settings.cache_clear()
    try:
        no_token = await _request(client)
        assert no_token.status_code == 400
        assert no_token.json()["code"] == "signup.turnstile_failed"
    finally:
        monkeypatch.delenv("TURNSTILE_SECRET")
        get_settings.cache_clear()


async def test_language_propagates_to_the_agency(
    client: AsyncClient, db_session: AsyncSession, sent: list[dict]
) -> None:
    await _request(client, lang="en")
    assert "verification code" in sent[-1]["subject"].lower()  # le mail suit la langue
    token = (await _verify(client)).json()["completion_token"]
    assert (await _complete(client, token, language="en")).status_code == 200
    agency = (
        await db_session.execute(select(Agency).where(Agency.name == "Neo Agence"))
    ).scalar_one()
    assert agency.default_language == "en"


async def test_nurture_skips_non_french_agencies(
    db_session: AsyncSession, sync_session_local, make_agency
) -> None:
    """DECISION self-serve : un signup EN ne recoit RIEN du nurture plutot
    que du francais tutoye."""
    from src.nurture.nurture_job import send_trial_nurture

    en_agency = await make_agency(slug="english-only")
    await db_session.execute(
        update(Agency)
        .where(Agency.id == en_agency.id)
        .values(default_language="en", trial_ends_at=datetime.now(UTC) + timedelta(days=20))
    )
    await db_session.commit()
    with sync_session_local() as sync_db:
        stats = send_trial_nurture(sync_db, log=lambda m: None, dry_run=True)
    # L'agence EN n'entre meme pas dans le scope du job.
    assert "english-only" not in str(stats)


# --- sectors mandatory & atomic at signup (2026-07-21) --------------------------------


async def test_signup_complete_without_sectors_is_422(client: AsyncClient) -> None:
    assert (await _request(client)).status_code == 200
    token = (await _verify(client)).json()["completion_token"]
    resp = await _complete(client, token, sectors=None)  # omitted entirely
    assert resp.status_code == 422
    assert "signup.sectors_required" in resp.text


async def test_signup_complete_empty_sectors_is_422(client: AsyncClient) -> None:
    assert (await _request(client)).status_code == 200
    token = (await _verify(client)).json()["completion_token"]
    resp = await _complete(client, token, sectors=[])
    assert resp.status_code == 422
    assert "signup.sectors_required" in resp.text


async def test_signup_complete_invalid_sector_is_422(client: AsyncClient) -> None:
    assert (await _request(client)).status_code == 200
    token = (await _verify(client)).json()["completion_token"]
    resp = await _complete(client, token, sectors=["banking"])  # not in the enum
    assert resp.status_code == 422
    assert "agency.sector_invalid" in resp.text


async def test_signup_invalid_sector_creates_no_orphan_agency(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Atomicity: a bad sector fails BEFORE any DB write — no orphan agency
    without a sector, and the completion token is NOT consumed."""
    from shared.models.agency import Agency

    assert (await _request(client)).status_code == 200
    token = (await _verify(client)).json()["completion_token"]
    resp = await _complete(client, token, sectors=["banking"])
    assert resp.status_code == 422

    # Nothing created.
    agency = (
        await db_session.execute(select(Agency).where(Agency.name == "Neo Agence"))
    ).scalar_one_or_none()
    assert agency is None
    # The token survives → the user can retry with a valid sector.
    retry = await _complete(client, token, sectors=["legal"])
    assert retry.status_code == 200


async def test_signup_unknown_field_is_422_not_swallowed(client: AsyncClient) -> None:
    """extra=forbid on /signup: a field that belongs elsewhere (sectors →
    /signup/complete) is a LOUD 422, never silently ignored. This is the
    root-cause hardening of the 'sectors swallowed' confusion."""
    resp = await client.post(
        "/signup",
        json={"email": "x@example.com", "lang": "fr", "sectors": ["legal"]},
    )
    assert resp.status_code == 422  # extra=forbid bites — no silent swallow


async def test_signup_and_complete_have_sector_parity(client: AsyncClient) -> None:
    """Parity: /signup does NOT create an agency (stage 1, no sectors), and
    /signup/complete is the single self-serve creation point where sectors
    are mandatory + atomic (already covered above)."""
    # /signup rejects sectors (they belong to complete).
    r1 = await client.post(
        "/signup", json={"email": "parity@example.com", "lang": "fr", "sectors": ["legal"]}
    )
    assert r1.status_code == 422
    # The full flow WITHOUT the stray field works and persists sectors.
    assert (await _request(client, email="parity2@example.com")).status_code == 200
    token = (await _verify(client, email="parity2@example.com")).json()["completion_token"]
    ok = await _complete(client, token, sectors=["accounting"])
    assert ok.status_code == 200

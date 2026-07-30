"""Lot notification « document signé » (30/07).

À la COMPLÉTION d'une demande (le form.completed FINAL, contreseing
compris — jamais aux partielles) : un mail à CHAQUE signataire client à
l'email connu, dans SA langue, sans pièce jointe (le lien mène à l'espace
où le PDF signé et le dossier de preuve sont déjà archivés) ; la face
agence est prévenue aussi (constat du lot : rien de direct n'existait).
Idempotent face aux rejeux (convergence existante : _complete ne court
qu'une fois). Signataire sans email/compte → silencieux."""

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from shared.models.expat_user import ExpatUser
from shared.models.rbac import Role
from src.core import email
from src.core.config import get_settings
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent
from tests.plugins.case_plugin import MakeClientCase
from tests.plugins.expat_plugin import MakeExpatUser
from tests.plugins.signature_plugin import FakeProvider
from tests.test_signatures import _requests, _signable_case, _signers
from tests.test_signatures_countersign import _sign, _signable_countersign_case

pytestmark = pytest.mark.usefixtures("rbac_baseline", "signatures_enabled")


@pytest_asyncio.fixture
async def admin(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    return await make_agent(role=system_roles["admin"])


def _signed_mails() -> list:
    """Les mails « document signé » de l'outbox, toutes langues (les
    sujets ×7 partagent le marqueur du template, pas un mot français)."""
    markers = (
        "est signé",
        "is signed",
        "está firmado",
        "подписан",
        "assinado",
        "firmato",
        "aláírva",
    )
    return [m for m in email.outbox if any(k in m.subject for k in markers)]


def _agency_signed_mails() -> list:
    markers = (
        "toutes les parties",
        "all parties",
        "todas las partes",
        "всеми сторонами",
        "todas as partes",
        "tutte le parti",
        "minden fél",
    )
    return [m for m in email.outbox if any(k in m.subject for k in markers)]


async def test_completion_notifies_each_client_in_their_language(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
    fake_provider: FakeProvider,
    give_credits,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await give_credits(admin.agency_id, 10)
    case, _pid = await _signable_case(
        client,
        db_session,
        admin,
        agent_headers(admin),
        make_client_case,
        make_expat_user,
        email_prefix="notif",
    )
    case_id = case.id
    # Le principal préfère l'espagnol — le mail doit suivre SA langue.
    principal = (
        await db_session.execute(select(ExpatUser).where(ExpatUser.email == "notif-p@example.com"))
    ).scalar_one()
    principal.preferred_lang = "es"
    await db_session.commit()

    request = (await _requests(db_session, case_id))[0]
    signer_ids = [s.id for s in await _signers(db_session, request.id)]
    monkeypatch.setenv("DOCUSEAL_WEBHOOK_SECRET", "whsec-notif")
    get_settings.cache_clear()
    # Partielle : ZÉRO notification.
    await _sign(client, signer_ids[0], "whsec-notif")
    assert _signed_mails() == []
    # Complétion : un mail par signataire client, dans SA langue + la face
    # agence (owner) prévenue.
    await _sign(client, signer_ids[1], "whsec-notif")
    mails = _signed_mails()
    by_to = {m.to: m for m in mails}
    assert set(by_to) == {"notif-p@example.com", "notif-m@example.com"}
    assert "está firmado" in by_to["notif-p@example.com"].subject  # es
    assert "est signé" in by_to["notif-m@example.com"].subject  # défaut agence fr
    agency_mails = _agency_signed_mails()
    assert [m.to for m in agency_mails] == [admin.email]

    # Rejeu ×3 du webhook final : toujours le même compte (convergence).
    for _ in range(3):
        await _sign(client, signer_ids[1], "whsec-notif")
    assert len(_signed_mails()) == 2
    assert len(_agency_signed_mails()) == 1


async def test_countersign_completion_notifies_and_skips_emailless_member(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
    fake_provider: FakeProvider,
    give_credits,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Contreseing : la complétion attend la signature AGENCE ; le membre
    SANS email (garde-fou) est sauté en silence — le principal et la face
    agence sont prévenus."""
    await give_credits(admin.agency_id, 10)
    headers = agent_headers(admin)
    case_id, _pid = await _signable_countersign_case(
        client, admin, headers, make_client_case, make_expat_user, fake_provider, "ntfcs"
    )
    request = (await _requests(db_session, case_id))[0]
    signers = await _signers(db_session, request.id)
    client_ids = [s.id for s in signers if s.agent_id is None]
    agency_seat_id = next(s.id for s in signers if s.agent_id is not None)
    monkeypatch.setenv("DOCUSEAL_WEBHOOK_SECRET", "whsec-ntfcs")
    get_settings.cache_clear()
    for sid in client_ids:
        await _sign(client, sid, "whsec-ntfcs")
    # Tous les clients ont signé, l'agence PAS ENCORE : demande non
    # complétée → aucune notification « signé ».
    assert _signed_mails() == []
    await _sign(client, agency_seat_id, "whsec-ntfcs")
    mails = _signed_mails()
    # Le membre « Membre CS » n'a pas d'email : silencieux — un seul mail
    # client (le principal), plus la face agence.
    assert [m.to for m in mails] == ["ntfcs-p@example.com"]
    assert [m.to for m in _agency_signed_mails()] == [admin.email]

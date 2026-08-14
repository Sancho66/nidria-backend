"""L'acquisition traçable + le contact de l'agence (lot 13/08).

Le front capture les UTM à l'atterrissage sur /signup (première touche
gagnante, gardés le temps de l'onglet) mais REFUSAIT de les émettre : les
schémas étaient en extra="forbid", donc un envoi aurait rendu 422 — et les
accepter sans les stocker aurait été pire, un no-op silencieux qui aurait
l'air de marcher pendant qu'on jette la source à chaque inscription.

Ce fichier grave le contrat de bout en bout : les quatre champs acceptés aux
DEUX étapes, la première touche qui l'emporte, la source servie aux trois
endroits (liste admin, fiche, alerte interne), et le téléphone de contact qui
rejoint l'identité légale — colonne, jeton, segment du modèle généré."""

import uuid

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agency import Agency
from shared.models.agent import Agent
from shared.models.rbac import Role
from src.consents.agency_template import generate_client_privacy, generate_client_terms
from src.consents.agency_tokens import resolve, token_values, unfilled_tokens
from src.core import ratelimit
from src.core.config import get_settings
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent

pytestmark = pytest.mark.usefixtures("rbac_baseline")

CAMPAIGN = {
    "utm_source": "newsletter",
    "utm_medium": "email",
    "utm_campaign": "lancement-aout",
    "referrer": "https://www.google.com/",
}


@pytest.fixture(autouse=True)
def _signup_harness(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("src.signup.signup_manager._generate_code", lambda: "123456")
    monkeypatch.setattr("src.signup.signup_manager.send_email", lambda *a, **kw: None)
    box: list[dict] = []
    monkeypatch.setattr(
        "src.signup.signup_alert.send_email",
        lambda to, subject, body, html=None, **kw: box.append({"to": to, "body": body}),
    )
    ratelimit.reset()
    yield box
    ratelimit.reset()


@pytest.fixture
def alerts(_signup_harness) -> list[dict]:
    return _signup_harness


@pytest_asyncio.fixture
async def superadmin(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    return await make_agent(role=system_roles["superadmin"], email="root@platform.io")


async def _request(client: AsyncClient, email: str = "neo@agence.io", **acq):
    return await client.post("/signup", json={"email": email, "lang": "fr", **acq})


async def _complete(client: AsyncClient, email: str = "neo@agence.io", **acq):
    token = (await client.post("/signup/verify", json={"email": email, "code": "123456"})).json()[
        "completion_token"
    ]
    return await client.post(
        "/signup/complete",
        json={
            "completion_token": token,
            "agency_name": "Neo Agence",
            "first_name": "Neo",
            "last_name": "Fondateur",
            "password": "MotDePasse1!",
            "language": "fr",
            "sectors": ["legal"],
            **acq,
        },
    )


async def _agency(db: AsyncSession) -> Agency:
    db.expire_all()
    return (await db.execute(select(Agency).where(Agency.slug.like("neo%")))).scalars().one()


async def _admin_row(client: AsyncClient, headers: dict[str, str], agency_id: uuid.UUID) -> dict:
    body = (await client.get("/admin/agencies?page_size=100", headers=headers)).json()
    return next(r for r in body["items"] if r["id"] == str(agency_id))


# --- le contrat : les quatre champs, aux DEUX étapes ---------------------------------


async def test_the_first_step_carries_the_campaign_all_the_way_to_the_agency(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """L'étape 1 est la première touche — celle qui compte. L'inscription se
    termine trois écrans plus loin, sur des URL qui ne portent plus rien."""
    assert (await _request(client, **CAMPAIGN)).status_code == 200
    assert (await _complete(client)).status_code == 200

    agency = await _agency(db_session)
    assert agency.utm_source == "newsletter"
    assert agency.utm_medium == "email"
    assert agency.utm_campaign == "lancement-aout"
    assert agency.referrer == "https://www.google.com/"
    # La première touche est datée, et distincte de la création du compte.
    assert agency.acquisition_captured_at is not None
    assert agency.acquisition_captured_at <= agency.created_at


async def test_the_last_step_alone_still_carries_it(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Filet : si l'onglet a été rechargé, le POST final reste accepté."""
    assert (await _request(client)).status_code == 200
    assert (await _complete(client, **CAMPAIGN)).status_code == 200
    assert (await _agency(db_session)).utm_campaign == "lancement-aout"


async def test_the_first_touch_wins_over_a_later_one(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Un visiteur venu par la campagne puis revenu sur un /signup nu ne
    s'efface pas lui-même — la doctrine du front, tenue côté serveur."""
    assert (await _request(client, **CAMPAIGN)).status_code == 200
    assert (await _complete(client, utm_source="direct", utm_campaign=None)).status_code == 200
    agency = await _agency(db_session)
    assert agency.utm_source == "newsletter"
    assert agency.utm_campaign == "lancement-aout"


async def test_asking_for_a_new_code_does_not_erase_the_source(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """La re-demande SUPPRIME la vérification précédente : sa source est
    récupérée avant, sinon « renvoyez-moi le code » coûterait la campagne."""
    assert (await _request(client, **CAMPAIGN)).status_code == 200
    assert (await _request(client)).status_code == 200  # re-demande, sans UTM
    assert (await _complete(client)).status_code == 200
    assert (await _agency(db_session)).utm_source == "newsletter"


async def test_a_direct_signup_stays_valid_and_carries_nothing(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Une inscription directe est un FAIT, pas un trou — et pas une erreur."""
    assert (await _request(client)).status_code == 200
    assert (await _complete(client)).status_code == 200
    agency = await _agency(db_session)
    assert agency.utm_source is None
    assert agency.acquisition_captured_at is None


async def test_the_public_entry_is_bounded_and_sanitized(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Entrée PUBLIQUE non authentifiée : la validation du front n'est pas une
    garantie. Sauts de ligne aplatis, bornage dur, champ creux → None."""
    assert (
        await _request(
            client,
            utm_source="  news\nletter  ",
            utm_campaign="x" * 500,
            utm_medium="   ",
        )
    ).status_code == 200
    assert (await _complete(client)).status_code == 200
    agency = await _agency(db_session)
    assert agency.utm_source == "news letter"
    assert len(agency.utm_campaign or "") == 200
    assert agency.utm_medium is None


# --- servi aux TROIS endroits --------------------------------------------------------


async def test_the_admin_row_serves_the_source_and_the_contact(
    client: AsyncClient,
    db_session: AsyncSession,
    superadmin: Agent,
    agent_headers: AuthHeaders,
) -> None:
    """La liste ET la fiche du front se construisent de cette ligne."""
    # L'en-tête AVANT le expire_all() de _agency : sinon l'accès à
    # superadmin.id déclencherait un rafraîchissement hors contexte async.
    headers = agent_headers(superadmin)
    assert (await _request(client, **CAMPAIGN)).status_code == 200
    assert (await _complete(client)).status_code == 200
    agency = await _agency(db_session)

    row = await _admin_row(client, headers, agency.id)
    assert row["utm_source"] == "newsletter"
    assert row["utm_medium"] == "email"
    assert row["utm_campaign"] == "lancement-aout"
    assert row["referrer"] == "https://www.google.com/"
    assert row["acquisition_captured_at"] is not None
    # De quoi RAPPELER : le propriétaire, c'est le premier agent interne.
    assert row["owner_name"] == "Neo Fondateur"
    assert row["owner_email"] == "neo@agence.io"
    # owner_phone n'est PAS servi : `agent` n'a pas de colonne téléphone.
    assert "owner_phone" not in row
    assert "contact_phone" in row


async def test_the_alert_names_the_real_source(client: AsyncClient, alerts, monkeypatch) -> None:
    """C'était l'information la plus utile du mail, et il disait « inconnue »."""
    settings = get_settings()
    monkeypatch.setattr(settings, "signup_alert_enabled", True)
    monkeypatch.setattr(settings, "signup_alert_recipients", ["equipe@interne.test"])

    assert (await _request(client, **CAMPAIGN)).status_code == 200
    assert (await _complete(client)).status_code == 200

    body = alerts[0]["body"]
    assert "source newsletter" in body
    assert "support email" in body
    assert "campagne lancement-aout" in body
    assert "https://www.google.com/" in body
    assert "inconnue" not in body


async def test_the_alert_still_says_unknown_for_a_direct_signup(
    client: AsyncClient, alerts, monkeypatch
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "signup_alert_enabled", True)
    monkeypatch.setattr(settings, "signup_alert_recipients", ["equipe@interne.test"])
    assert (await _request(client)).status_code == 200
    assert (await _complete(client)).status_code == 200
    assert "Source     : inconnue" in alerts[0]["body"]


# --- le téléphone rejoint l'identité légale ------------------------------------------


async def test_contact_phone_is_served_and_written_like_its_neighbours(
    client: AsyncClient, make_agent: MakeAgent, system_roles, agent_headers: AuthHeaders
) -> None:
    admin = await make_agent(role=system_roles["admin"])
    headers = agent_headers(admin)

    before = (await client.get("/agencies/me", headers=headers)).json()
    assert before["contact_phone"] is None

    patched = await client.patch(
        "/agencies/me", headers=headers, json={"contact_phone": "+33 1 02 03 04 05"}
    )
    assert patched.status_code == 200, patched.text
    after = (await client.get("/agencies/me", headers=headers)).json()
    assert after["contact_phone"] == "+33 1 02 03 04 05"


async def test_the_phone_is_a_token_of_the_catalogue(
    make_agent: MakeAgent, system_roles, db_session: AsyncSession
) -> None:
    """Le dixième jeton : résolu à la lecture, blanc si vide, présent au
    contrat avec son libellé et sa valeur courante."""
    admin = await make_agent(role=system_roles["admin"])
    agency = await db_session.get(Agency, admin.agency_id)
    assert agency is not None

    names = [name for name, _label, _value in token_values(agency)]
    assert "contact_phone" in names
    label = next(lbl for name, lbl, _v in token_values(agency) if name == "contact_phone")
    assert label == "votre téléphone de contact"

    # Vide → blanc à la lecture, et signalé à l'édition.
    assert resolve("Appelez le {contact_phone}.", agency) == "Appelez le ."
    assert "{contact_phone}" in unfilled_tokens(agency, "Appelez le {contact_phone}.")

    agency.contact_phone = "01 02 03 04 05"
    assert resolve("Appelez le {contact_phone}.", agency) == "Appelez le 01 02 03 04 05."
    assert unfilled_tokens(agency, "Appelez le {contact_phone}.") == []


def test_the_generator_stays_grammatical_with_the_new_segment() -> None:
    """L'omission par segment tient au degré de remplissage 0, 1, … n — le
    téléphone compris. UN SEUL segment « joignable » porte email ET
    téléphone : deux segments donneraient « joignable à …, joignable au … »."""
    only_phone = generate_client_terms(Agency(name="ACME", slug="acme", contact_phone="01 02"))
    assert "joignable au 01 02" in only_phone
    assert "joignable à " not in only_phone

    only_email = generate_client_terms(Agency(name="ACME", slug="acme", contact_email="a@b.fr"))
    assert "joignable à a@b.fr" in only_email
    assert " au " not in only_email

    both = generate_client_terms(
        Agency(name="ACME", slug="acme", contact_email="a@b.fr", contact_phone="01 02")
    )
    assert "joignable à a@b.fr et au 01 02" in both

    neither = generate_client_terms(Agency(name="ACME", slug="acme"))
    assert "joignable" not in neither

    # Progressif : aucun texte ne doit jamais bégayer ni pendre.
    steps: list[dict[str, str]] = [
        {},
        {"contact_phone": "01 02"},
        {"contact_email": "a@b.fr", "contact_phone": "01 02"},
        {
            "legal_name": "ACME SARL",
            "legal_form": "SAS",
            "registration_number": "RCS 123",
            "address": "1 rue de la Paix",
            "city": "Paris",
            "postal_code": "75001",
            "country": "FR",
            "contact_email": "a@b.fr",
            "contact_phone": "01 02",
        },
    ]
    for fields in steps:
        agency = Agency(name="ACME", slug="acme", **fields)
        for text in (generate_client_terms(agency), generate_client_privacy(agency)):
            assert "[" not in text and "]" not in text
            assert ", ," not in text and ",," not in text
            assert "au ." not in text and "à ." not in text
            assert "{agency_name}" in text


async def test_an_empty_phone_is_named_among_the_missing_legal_fields(
    make_agent: MakeAgent, system_roles, db_session: AsyncSession
) -> None:
    from src.consents.agency_template import missing_legal_fields

    admin = await make_agent(role=system_roles["admin"])
    agency = await db_session.get(Agency, admin.agency_id)
    assert agency is not None
    assert "contact_phone" in missing_legal_fields(agency)
    agency.contact_phone = "01 02"
    assert "contact_phone" not in missing_legal_fields(agency)

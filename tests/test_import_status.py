"""LE STATUT À L'IMPORT — et le rattrapage qui le rend réversible.

Trois règles tiennent ce lot, et chacune a son test :

1. Le statut voulu se pose sur les fiches CRÉÉES, jamais sur les liées —
   lier ne requalifie pas, une fiche qui existe garde le sien.
2. La colonne prime sur le défaut global ; le défaut ne comble que les
   lignes muettes. Une cellule illisible est un trou motivé, pas une
   requalification au hasard : la ligne vit.
3. Le geste est REPRENABLE en masse. Sans ça, un défaut posé de travers
   sur 1600 lignes ne se rattrape qu'à la main, fiche par fiche.
"""

import uuid

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from shared.models.client_profile import ClientProfile
from shared.models.rbac import Role
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent

pytestmark = pytest.mark.usefixtures("rbac_baseline")


@pytest_asyncio.fixture
async def admin(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    return await make_agent(role=system_roles["admin"])


async def _statuses(db: AsyncSession, agency_id: uuid.UUID) -> dict[str, str | None]:
    rows = (
        (
            await db.execute(
                select(ClientProfile.email, ClientProfile.status_override).where(
                    ClientProfile.agency_id == agency_id
                )
            )
        )
        .tuples()
        .all()
    )
    return {email or "": override for email, override in rows}


# --- le statut voulu ------------------------------------------------------------------


async def test_default_status_lands_on_created_profiles_and_is_counted(
    client: AsyncClient, db_session: AsyncSession, admin: Agent, agent_headers: AuthHeaders
) -> None:
    """« 1593 créées dont 1593 en client » : le rapport le dit."""
    csv_text = "Prénom,Nom,Email\nAna,Silva,ana@example.com\nBruno,Costa,bruno@example.com\n"
    r = await client.post(
        "/imports/client-profiles",
        headers=agent_headers(admin),
        json={
            "csv_text": csv_text,
            "mapping": {"Prénom": "first_name", "Nom": "last_name", "Email": "email"},
            "default_status": "client",
        },
    )
    assert r.status_code == 200, r.text
    report = r.json()
    assert len(report["created"]) == 2
    assert report["created_by_status"] == {"client": 2}
    assert set((await _statuses(db_session, admin.agency_id)).values()) == {"client"}


async def test_without_default_status_nothing_is_posed(
    client: AsyncClient, db_session: AsyncSession, admin: Agent, agent_headers: AuthHeaders
) -> None:
    """Absent = la dérivation joue, comme avant ce lot. On ne change pas
    une valeur par défaut en silence."""
    r = await client.post(
        "/imports/client-profiles",
        headers=agent_headers(admin),
        json={
            "csv_text": "Prénom,Nom,Email\nCarla,Dias,carla@example.com\n",
            "mapping": {"Prénom": "first_name", "Nom": "last_name", "Email": "email"},
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["created_by_status"] == {}
    assert (await _statuses(db_session, admin.agency_id))["carla@example.com"] is None


async def test_linking_never_requalifies_an_existing_profile(
    client: AsyncClient, db_session: AsyncSession, admin: Agent, agent_headers: AuthHeaders
) -> None:
    """LA RÈGLE du lot : une fiche existante garde son statut. Ni le
    défaut global ni la colonne ne le touchent — sinon un ré-import
    requalifierait tout un annuaire sans que personne l'ait demandé."""
    headers = agent_headers(admin)
    created = await client.post(
        "/client-profiles",
        headers=headers,
        json={"first_name": "Déjà", "last_name": "Là", "email": "deja@example.com"},
    )
    assert created.status_code == 201, created.text
    profile_id = created.json()["id"]

    r = await client.post(
        "/imports/client-profiles",
        headers=headers,
        json={
            "csv_text": "Prénom,Nom,Email,Statut\nDéjà,Là,deja@example.com,client\n",
            "mapping": {
                "Prénom": "first_name",
                "Nom": "last_name",
                "Email": "email",
                "Statut": "status_override",
            },
            "default_status": "client",
        },
    )
    assert r.status_code == 200, r.text
    assert [x["profile_id"] for x in r.json()["linked"]] == [profile_id]
    assert r.json()["created_by_status"] == {}  # rien n'a été créé, rien n'est posé
    assert (await _statuses(db_session, admin.agency_id))["deja@example.com"] is None


async def test_the_column_wins_and_the_default_fills_the_silent_rows(
    client: AsyncClient, db_session: AsyncSession, admin: Agent, agent_headers: AuthHeaders
) -> None:
    """La colonne dit le statut ligne par ligne ; le défaut ne comble que
    les lignes muettes. Une cellule illisible reste un TROU — la ligne
    vit, elle n'est pas requalifiée au hasard."""
    csv_text = (
        "Prénom,Nom,Email,Statut\n"
        "Ana,Silva,ana@example.com,Client\n"  # forme CRM
        "Bruno,Costa,bruno@example.com,lead\n"  # synonyme de prospect
        "Carla,Dias,carla@example.com,\n"  # muette → le défaut
        "Dan,Roux,dan@example.com,peut-être\n"  # illisible → trou
    )
    r = await client.post(
        "/imports/client-profiles",
        headers=agent_headers(admin),
        json={
            "csv_text": csv_text,
            "mapping": {
                "Prénom": "first_name",
                "Nom": "last_name",
                "Email": "email",
                "Statut": "status_override",
            },
            "default_status": "prospect",
        },
    )
    assert r.status_code == 200, r.text
    report = r.json()
    assert len(report["created"]) == 4  # la ligne illisible VIT
    assert report["created_by_status"] == {"client": 1, "prospect": 3}

    statuses = await _statuses(db_session, admin.agency_id)
    assert statuses["ana@example.com"] == "client"
    assert statuses["bruno@example.com"] == "prospect"
    assert statuses["carla@example.com"] == "prospect"  # le défaut
    # Illisible : la colonne n'a rien dit, le DÉFAUT prend le relais —
    # jamais une valeur devinée depuis « peut-être ».
    assert statuses["dan@example.com"] == "prospect"


async def test_an_unreadable_status_is_reported_as_an_issue_in_the_preview(
    client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    """L'aperçu NOMME la cellule fautive — l'agence voit ce que l'import
    n'a pas su lire, au lieu de le découvrir en base."""
    r = await client.post(
        "/imports/client-profiles/preview",
        headers=agent_headers(admin),
        json={
            "csv_text": "Prénom,Nom,Email,Statut\nDan,Roux,dan@example.com,peut-être\n",
            "mapping": {
                "Prénom": "first_name",
                "Nom": "last_name",
                "Email": "email",
                "Statut": "status_override",
            },
        },
    )
    assert r.status_code == 200, r.text
    row = r.json()["rows"][0]
    assert row["status"] == "create"
    assert {"column": "Statut", "code": "invalid_value"} in row["issues"]
    assert "status_override" not in row["person"]


async def test_the_status_column_is_suggested_and_previewed(
    client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    """La cible existe dans l'univers servi, et le suggéreur la propose —
    sans elle, le mapping par colonne serait injoignable depuis l'écran."""
    headers = agent_headers(admin)
    targets = (await client.get("/imports/targets?entity=person", headers=headers)).json()
    assert any(t["key"] == "status_override" for t in targets["targets"])

    suggestion = await client.post(
        "/imports/client-profiles/suggest-mapping",
        headers=headers,
        json={"headers": ["Prénom", "Nom", "Email", "Statut"]},
    )
    assert suggestion.json()["suggestions"]["Statut"] == "status_override"


# --- le rattrapage --------------------------------------------------------------------


async def test_bulk_reset_gives_the_profiles_back_to_the_derivation(
    client: AsyncClient, db_session: AsyncSession, admin: Agent, agent_headers: AuthHeaders
) -> None:
    """L'import mal réglé se rattrape en un geste : le dry-run annonce,
    l'exécution honore, et `with_override` ne compte que ce qui change."""
    headers = agent_headers(admin)
    await client.post(
        "/imports/client-profiles",
        headers=headers,
        json={
            "csv_text": (
                "Prénom,Nom,Email\nAna,Silva,ana@example.com\nBruno,Costa,bruno@example.com\n"
            ),
            "mapping": {"Prénom": "first_name", "Nom": "last_name", "Email": "email"},
            "default_status": "client",
        },
    )
    # Une fiche SANS override : elle est désignée par le filtre, mais elle
    # n'a rien à reprendre — le compte ne doit pas la gonfler.
    await client.post(
        "/client-profiles",
        headers=headers,
        json={"first_name": "Sans", "last_name": "Forcé", "email": "sans@example.com"},
    )

    dry = await client.post(
        "/client-profiles/bulk-reset-status",
        headers=headers,
        json={"filter": {}, "dry_run": True},
    )
    assert dry.status_code == 200, dry.text
    assert dry.json() == {"dry_run": True, "matching": 3, "with_override": 2, "reset": 0}
    assert sum(1 for v in (await _statuses(db_session, admin.agency_id)).values() if v) == 2

    run = await client.post(
        "/client-profiles/bulk-reset-status", headers=headers, json={"filter": {}}
    )
    assert run.status_code == 200, run.text
    assert run.json() == {"dry_run": False, "matching": 3, "with_override": 2, "reset": 2}
    assert set((await _statuses(db_session, admin.agency_id)).values()) == {None}


async def test_bulk_reset_by_ids_stays_inside_the_selection(
    client: AsyncClient, db_session: AsyncSession, admin: Agent, agent_headers: AuthHeaders
) -> None:
    """La forme `ids` ne déborde pas : les fiches hors sélection gardent
    leur statut."""
    headers = agent_headers(admin)
    await client.post(
        "/imports/client-profiles",
        headers=headers,
        json={
            "csv_text": (
                "Prénom,Nom,Email\nAna,Silva,ana@example.com\nBruno,Costa,bruno@example.com\n"
            ),
            "mapping": {"Prénom": "first_name", "Nom": "last_name", "Email": "email"},
            "default_status": "client",
        },
    )
    listing = (await client.get("/client-profiles?search=ana@", headers=headers)).json()
    target_id = listing["items"][0]["id"]

    r = await client.post(
        "/client-profiles/bulk-reset-status", headers=headers, json={"ids": [target_id]}
    )
    assert r.status_code == 200, r.text
    assert r.json()["reset"] == 1
    statuses = await _statuses(db_session, admin.agency_id)
    assert statuses["ana@example.com"] is None
    assert statuses["bruno@example.com"] == "client"


async def test_bulk_reset_refuses_an_ambiguous_selector(
    client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    """Ni les deux formes ensemble, ni aucune : on ne réinitialise pas
    tout par omission (même règle que la suppression de masse)."""
    headers = agent_headers(admin)
    both = await client.post(
        "/client-profiles/bulk-reset-status",
        headers=headers,
        json={"ids": [str(uuid.uuid4())], "filter": {}},
    )
    assert both.status_code == 422
    neither = await client.post("/client-profiles/bulk-reset-status", headers=headers, json={})
    assert neither.status_code == 422


async def test_the_preview_shows_the_status_each_row_will_get(
    client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    """LA GARANTIE du projet : l'aperçu montre ce que l'import écrira. Le
    défaut est donc posé dans l'ANALYSE — sinon l'écran annoncerait un
    statut et la base en recevrait un autre."""
    r = await client.post(
        "/imports/client-profiles/preview",
        headers=agent_headers(admin),
        json={
            "csv_text": (
                "Prénom,Nom,Email,Statut\n"
                "Ana,Silva,ana@example.com,Client\n"
                "Carla,Dias,carla@example.com,\n"
            ),
            "mapping": {
                "Prénom": "first_name",
                "Nom": "last_name",
                "Email": "email",
                "Statut": "status_override",
            },
            "default_status": "prospect",
        },
    )
    assert r.status_code == 200, r.text
    rows = {row["person"]["email"]: row for row in r.json()["rows"]}
    assert rows["ana@example.com"]["person"]["status_override"] == "client"
    assert rows["carla@example.com"]["person"]["status_override"] == "prospect"


async def test_the_preview_stays_silent_when_no_status_is_asked(
    client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    """Sans défaut ni colonne, l'aperçu n'invente pas un statut : la
    dérivation jouera, et elle ne se décide pas à l'import."""
    r = await client.post(
        "/imports/client-profiles/preview",
        headers=agent_headers(admin),
        json={
            "csv_text": "Prénom,Nom,Email\nAna,Silva,ana@example.com\n",
            "mapping": {"Prénom": "first_name", "Nom": "last_name", "Email": "email"},
        },
    )
    assert r.status_code == 200, r.text
    assert "status_override" not in r.json()["rows"][0]["person"]

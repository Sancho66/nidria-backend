"""Chantier fiches F1+F2 — backfill, lecture, fusion, gestes du croisement.

Phase 0 fait foi : une fiche par (agence, expat) ; latest-wins par champ ;
sans-compte hors fiche ; la fiche copie, ne déporte jamais ; divergence
par comparaison à la lecture ; gestes-péages tracés ; scopage agence
partout (non-révélation cross-agence)."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from shared.models.case_person import CasePerson
from shared.models.client_profile import ClientProfile
from shared.models.custom_field import CustomFieldDefinition
from shared.models.rbac import Role
from src.client_profiles.backfill import backfill_client_profiles
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent
from tests.plugins.case_plugin import MakeClientCase
from tests.plugins.expat_plugin import MakeExpatUser

pytestmark = pytest.mark.usefixtures("rbac_baseline")


@pytest_asyncio.fixture
async def admin(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    return await make_agent(role=system_roles["admin"])


async def _person_def(db: AsyncSession, agency_id: uuid.UUID, key: str) -> CustomFieldDefinition:
    row = CustomFieldDefinition(
        agency_id=agency_id, key=key, label=key, field_type="text", scope="person"
    )
    db.add(row)
    await db.flush()
    return row


# --- F1.4 BACKFILL --------------------------------------------------------------------


async def test_backfill_merges_clusters_latest_wins_and_is_idempotent(
    db_session: AsyncSession,
    admin: Agent,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
) -> None:
    """La grappe prod type : UN client, DEUX dossiers de la même agence →
    UNE fiche ; latest-wins PAR CHAMP (le récent écrase, l'ancien comble) ;
    la personne SANS compte reste hors fiche ; re-run = 0 création."""
    agency_id = admin.agency_id
    expat = await make_expat_user(activated=True, email="grappe@example.com")
    case_a = await make_client_case(agency_id=agency_id, principal_expat_user_id=expat.id)
    case_b = await make_client_case(agency_id=agency_id, principal_expat_user_id=expat.id)
    old_time = datetime.now(UTC) - timedelta(days=30)
    # La fixture crée déjà les principaux — on VALORISE les lignes réelles
    # (c'est la forme exacte des données prod que le backfill fusionnera).
    person_a = (
        await db_session.execute(
            select(CasePerson).where(
                CasePerson.case_id == case_a.id, CasePerson.kind == "principal"
            )
        )
    ).scalar_one()
    person_b = (
        await db_session.execute(
            select(CasePerson).where(
                CasePerson.case_id == case_b.id, CasePerson.kind == "principal"
            )
        )
    ).scalar_one()
    person_a.nationality = "FR"
    person_a.phone = "+33 6 00 00 00 01"
    person_a.custom_fields = {"birth_country": "FR", "visa_type": "vieux"}
    person_b.nationality = "PT"  # plus récent → GAGNE
    person_b.profession = "Notaire"  # absent de A → comble
    person_b.custom_fields = {"birth_country": "PT"}
    # Un membre SANS compte sur le dossier A : hors fiche, sans erreur.
    ghost = CasePerson(case_id=case_a.id, kind="family", full_name="Sans Compte")
    db_session.add(ghost)
    await db_session.commit()
    # L'ancienneté de A se force en SQL (updated_at est géré par le mixin).
    await db_session.execute(
        text("UPDATE case_person SET updated_at = :t WHERE id = :i"),
        {"t": old_time, "i": person_a.id},
    )
    await db_session.commit()

    stats = await db_session.run_sync(
        lambda sync_session: backfill_client_profiles(sync_session.connection())
    )
    await db_session.commit()
    assert stats["profiles_created"] >= 1
    assert stats["persons_without_account"] >= 1

    profile = (
        await db_session.execute(
            select(ClientProfile).where(
                ClientProfile.agency_id == agency_id, ClientProfile.expat_user_id == expat.id
            )
        )
    ).scalar_one()
    # Latest-wins par champ : nationalité du récent, téléphone de l'ancien
    # (comble), profession du récent, customs fusionnés récent-gagnant.
    assert profile.nationality == "PT"
    assert profile.phone == "+33 6 00 00 00 01"
    assert profile.profession == "Notaire"
    assert profile.custom_fields["birth_country"] == "PT"
    assert profile.custom_fields["visa_type"] == "vieux"
    # Les DEUX lignes principal liées ; le fantôme non.
    person_a_id, person_b_id, ghost_id = person_a.id, person_b.id, ghost.id
    profile_id = profile.id
    db_session.expire_all()
    for pid in (person_a_id, person_b_id):
        row = await db_session.get(CasePerson, pid)
        assert row is not None and row.client_profile_id == profile_id
    ghost_row = await db_session.get(CasePerson, ghost_id)
    assert ghost_row is not None and ghost_row.client_profile_id is None

    # IDEMPOTENCE : re-run → 0 création, 0 nouvelle liaison.
    stats2 = await db_session.run_sync(
        lambda sync_session: backfill_client_profiles(sync_session.connection())
    )
    await db_session.commit()
    assert stats2["profiles_created"] == 0
    assert stats2["persons_linked"] == 0


# --- F1.5 LECTURE gated + F1.6 FUSION -------------------------------------------------


async def test_list_and_detail_gated_and_scoped(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
) -> None:
    headers = agent_headers(admin)
    # La création de dossier LIE et crée la fiche (hook F2.1).
    r = await client.post(
        "/cases",
        headers=headers,
        json={"first_name": "Fiche", "last_name": "Un", "email": "fiche1@example.com"},
    )
    assert r.status_code == 201, r.text
    listing = (await client.get("/client-profiles?search=fiche1", headers=headers)).json()
    assert listing["total"] == 1
    item = listing["items"][0]
    assert item["email"] == "fiche1@example.com"
    assert item["derived_status"] == "client"  # règle actée : dossier vivant → client
    detail = (await client.get(f"/client-profiles/{item['id']}", headers=headers)).json()
    assert detail["email"] == "fiche1@example.com"
    assert len(detail["cases"]) == 1
    assert detail["completeness"]["missing"]  # rien de valorisé encore
    # Cross-agence : une autre agence ne voit RIEN (404 non-révélateur).
    other = await make_agent(role=system_roles["admin"])
    r = await client.get(f"/client-profiles/{item['id']}", headers=agent_headers(other))
    assert r.status_code == 404
    assert (
        await client.get("/client-profiles?search=fiche1", headers=agent_headers(other))
    ).json()["total"] == 0


async def test_merge_relinks_fills_gaps_and_traces(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
) -> None:
    """FUSIONNER : deux fiches (deux emails du même humain) → une ; la
    cible gagne, la source comble ; les case_person re-liés ; tracé sur
    chaque dossier ; la source disparaît."""
    headers = agent_headers(admin)
    for i, email_addr in enumerate(("dup-a@example.com", "dup-b@example.com")):
        r = await client.post(
            "/cases",
            headers=headers,
            json={"first_name": f"Dup{i}", "last_name": "Client", "email": email_addr},
        )
        assert r.status_code == 201, r.text
    listing = (await client.get("/client-profiles?search=dup-", headers=headers)).json()
    assert listing["total"] == 2
    by_email = {i["email"]: i for i in listing["items"]}
    target_id = by_email["dup-a@example.com"]["id"]
    source_id = by_email["dup-b@example.com"]["id"]
    # Valeurs : la source porte une nationalité, la cible non → comblée.
    source_row = await db_session.get(ClientProfile, uuid.UUID(source_id))
    assert source_row is not None
    source_row.nationality = "BR"
    await db_session.commit()
    r = await client.post(
        f"/client-profiles/{target_id}/merge",
        headers=headers,
        json={"source_profile_id": source_id},
    )
    assert r.status_code == 200, r.text
    assert r.json()["nationality"] == "BR"
    await db_session.rollback()
    assert await db_session.get(ClientProfile, uuid.UUID(source_id)) is None
    # Les personnes de la source pointent la cible.
    relinked = (
        (
            await db_session.execute(
                select(CasePerson).where(CasePerson.client_profile_id == uuid.UUID(target_id))
            )
        )
        .scalars()
        .all()
    )
    assert len(relinked) == 2
    # Le tracé vit sur les dossiers re-liés.
    n_logs = (
        await db_session.execute(
            text("SELECT count(*) FROM activity_log WHERE action_type = 'profile.merged'")
        )
    ).scalar_one()
    assert n_logs >= 1
    # Auto-fusion refusée.
    r = await client.post(
        f"/client-profiles/{target_id}/merge",
        headers=headers,
        json={"source_profile_id": target_id},
    )
    assert r.status_code == 422
    assert r.json()["code"] == "profile.merge_self"


# --- F2.1 pré-remplissage + F2.2 divergence + F2.3 gestes -----------------------------


async def test_prefill_divergence_and_both_gestures(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
) -> None:
    """Le cycle complet du croisement : dossier 1 valorise → promotion vers
    la fiche → dossier 2 naît PRÉ-REMPLI (snapshot) → il diverge (édition
    locale) → le pull réaligne. La promotion ne touche JAMAIS les autres
    dossiers."""
    headers = agent_headers(admin)
    agency_id = admin.agency_id
    await _person_def(db_session, agency_id, "birth_country")
    await db_session.commit()

    # Dossier 1 : le wizard pose nationalité + custom person.
    r = await client.post(
        "/cases",
        headers=headers,
        json={
            "first_name": "Croise",
            "last_name": "Ment",
            "email": "croisement@example.com",
            "nationality": "FR",
            "custom_fields": {"birth_country": "FR"},
        },
    )
    assert r.status_code == 201, r.text
    case1 = r.json()
    detail1 = (await client.get(f"/cases/{case1['id']}", headers=headers)).json()
    person1 = detail1["persons"][0]
    assert person1["client_profile_id"]  # lié à sa fiche dès la création
    profile_id = person1["client_profile_id"]
    assert person1["differs_from_profile"] == []  # fiche vide : rien ne diverge

    # PROMOTION des deux champs vers la fiche.
    for reference in ("nationality", "birth_country"):
        r = await client.post(
            f"/cases/{case1['id']}/persons/{person1['id']}/promote-field",
            headers=headers,
            json={"reference": reference},
        )
        assert r.status_code == 200, r.text
    completeness = (
        await client.get(f"/client-profiles/{profile_id}/completeness", headers=headers)
    ).json()
    assert "nationality" in completeness["filled"]
    assert "birth_country" in completeness["filled"]

    # Dossier 2 : né PRÉ-REMPLI depuis la fiche (« Nouvelle démarche »).
    r = await client.post(f"/client-profiles/{profile_id}/cases", headers=headers, json={})
    assert r.status_code in (200, 201), r.text
    case2_id = r.json()["id"]
    detail2 = (await client.get(f"/cases/{case2_id}", headers=headers)).json()
    person2 = detail2["persons"][0]
    assert person2["nationality"] == "FR"  # snapshot posé
    assert person2["custom_fields"]["birth_country"] == "FR"
    assert person2["differs_from_profile"] == []

    # Divergence : le dossier 2 change SA valeur localement.
    r = await client.patch(
        f"/cases/{case2_id}/persons/{person2['id']}",
        headers=headers,
        json={"nationality": "PT"},
    )
    assert r.status_code == 200, r.text
    detail2 = (await client.get(f"/cases/{case2_id}", headers=headers)).json()
    person2 = detail2["persons"][0]
    assert person2["differs_from_profile"] == ["nationality"]
    # Le dossier 1, lui, n'a pas bougé (la promotion n'écrit QUE la fiche).
    detail1 = (await client.get(f"/cases/{case1['id']}", headers=headers)).json()
    assert detail1["persons"][0]["nationality"] == "FR"
    assert detail1["persons"][0]["differs_from_profile"] == []

    # GESTE 1 — promouvoir la nouvelle valeur : la fiche suit, le dossier 1
    # diverge à SON tour (sa valeur locale n'a pas bougé — structurel).
    r = await client.post(
        f"/cases/{case2_id}/persons/{person2['id']}/promote-field",
        headers=headers,
        json={"reference": "nationality"},
    )
    assert r.status_code == 200, r.text
    detail1 = (await client.get(f"/cases/{case1['id']}", headers=headers)).json()
    assert detail1["persons"][0]["differs_from_profile"] == ["nationality"]

    # GESTE 2 — reprendre depuis la fiche : le dossier 1 se réaligne.
    r = await client.post(
        f"/cases/{case1['id']}/persons/{person1['id']}/pull-field",
        headers=headers,
        json={"reference": "nationality"},
    )
    assert r.status_code == 200, r.text
    detail1 = (await client.get(f"/cases/{case1['id']}", headers=headers)).json()
    assert detail1["persons"][0]["nationality"] == "PT"
    assert detail1["persons"][0]["differs_from_profile"] == []

    # IDEMPOTENCE du geste : re-promouvoir la même valeur → 200, rien ne casse.
    r = await client.post(
        f"/cases/{case2_id}/persons/{person2['id']}/promote-field",
        headers=headers,
        json={"reference": "nationality"},
    )
    assert r.status_code == 200, r.text

    # Garde : une référence hors portée personne → 422 nommé.
    r = await client.post(
        f"/cases/{case1['id']}/persons/{person1['id']}/promote-field",
        headers=headers,
        json={"reference": "not_a_person_field"},
    )
    assert r.status_code == 422
    assert r.json()["code"] == "profile.reference_not_person_scope"


# --- ANNUAIRE F3 : ouvertures (views clients, filtres, coût de lecture) ---------------


async def test_views_open_to_clients_entity(
    client: AsyncClient,
    admin: Agent,
    agent_headers: AuthHeaders,
) -> None:
    """F3.1 — l'API des vues accepte l'entité 'clients' (persistance
    identique), la liste filtre par entité, le n'importe-quoi reste 422."""
    headers = agent_headers(admin)
    r = await client.post(
        "/views", headers=headers, json={"name": "Mes clients", "entity": "clients"}
    )
    assert r.status_code == 201, r.text
    assert r.json()["entity"] == "clients"
    listed = (await client.get("/views?entity=clients", headers=headers)).json()
    assert [v["name"] for v in listed] == ["Mes clients"]
    assert (await client.get("/views?entity=cases", headers=headers)).json() == []
    r = await client.post("/views", headers=headers, json={"name": "Bad", "entity": "unicorns"})
    assert r.status_code == 422


async def test_directory_list_filters_and_aggregates(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
) -> None:
    """F3.2 — le filtre status suit LA dérivation (jamais deux vérités :
    l'accord filtre SQL ↔ derived_status Python est vérifié item par item) ;
    chaque item porte tags, dernière activité, espace client activé."""
    headers = agent_headers(admin)
    # Prospect = fiche DIRECTE sans dossier ; client = fiche avec dossier
    # vivant (règle actée — même en statut prospect côté dossier).
    r = await client.post(
        "/client-profiles",
        headers=headers,
        json={"first_name": "Annuaire0", "last_name": "Test", "email": "annuaire-p@example.com"},
    )
    assert r.status_code == 201, r.text
    r = await client.post(
        "/cases",
        headers=headers,
        json={"first_name": "Annuaire1", "last_name": "Test", "email": "annuaire-c@example.com"},
    )
    assert r.status_code == 201, r.text
    all_items = (await client.get("/client-profiles?search=annuaire-", headers=headers)).json()
    assert all_items["total"] == 2
    for item in all_items["items"]:
        assert isinstance(item["tags"], list)
        assert item["last_activity_at"] is not None  # activité ou updated_at fiche
        assert item["client_space_activated"] is False  # jamais activé
    prospects = (
        await client.get("/client-profiles?search=annuaire-&status=prospect", headers=headers)
    ).json()
    clients_only = (
        await client.get("/client-profiles?search=annuaire-&status=client", headers=headers)
    ).json()
    # L'ACCORD : le filtre SQL renvoie exactement les items dont la
    # dérivation Python dit ce statut — et les deux moitiés se recomposent.
    assert {i["email"] for i in prospects["items"]} == {"annuaire-p@example.com"}
    assert {i["email"] for i in clients_only["items"]} == {"annuaire-c@example.com"}
    assert all(i["derived_status"] == "prospect" for i in prospects["items"])
    assert all(i["derived_status"] == "client" for i in clients_only["items"])
    assert prospects["total"] + clients_only["total"] == all_items["total"]


async def test_profile_case_summary_has_current_step_in_agency_language(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
) -> None:
    """F3.3 — la relation dossier de la fiche porte l'étape en cours,
    résolue dans la langue de l'agence."""
    headers = agent_headers(admin)
    r = await client.post("/journeys", headers=headers, json={"name": "Parcours annuaire"})
    assert r.status_code == 201, r.text
    template_id = r.json()["id"]
    r = await client.post(
        f"/journeys/{template_id}/steps", headers=headers, json={"name": "Première étape"}
    )
    assert r.status_code == 201, r.text
    r = await client.post(
        "/cases",
        headers=headers,
        json={
            "first_name": "Etape",
            "last_name": "Annuaire",
            "email": "etape-annuaire@example.com",
        },
    )
    assert r.status_code == 201, r.text
    # L'instanciation du progress passe par l'assignation (même règle que
    # la liste dossiers : sans progress, pas d'étape en cours).
    r = await client.post(
        f"/cases/{r.json()['id']}/journey",
        headers=headers,
        json={"journey_template_id": template_id},
    )
    assert r.status_code == 201, r.text
    listing = (await client.get("/client-profiles?search=etape-annuaire", headers=headers)).json()
    detail = (
        await client.get(f"/client-profiles/{listing['items'][0]['id']}", headers=headers)
    ).json()
    assert detail["cases"][0]["current_step_name"] == "Première étape"


async def test_directory_list_query_count_is_constant(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
) -> None:
    """F3.4 — témoin de comptage : la liste tourne en un nombre FIXE de
    requêtes groupées quel que soit le nombre de fiches (pas de N+1)."""
    from sqlalchemy import event

    from src.client_profiles.client_profiles_manager import ClientProfilesManager

    headers = agent_headers(admin)
    for i in range(3):
        r = await client.post(
            "/cases",
            headers=headers,
            json={"first_name": f"Fleet{i}", "last_name": "Witness", "email": f"w{i}@example.com"},
        )
        assert r.status_code == 201, r.text
    engine = db_session.get_bind()
    counter = {"n": 0}

    def _count(*_args: object, **_kwargs: object) -> None:
        counter["n"] += 1

    event.listen(engine, "before_cursor_execute", _count)
    try:
        result = await ClientProfilesManager(db_session).list_profiles(
            admin, search=None, status=None, page=1, page_size=20
        )
    finally:
        event.remove(engine, "before_cursor_execute", _count)
    assert result.total >= 3
    # 6 = items + count + dossiers + membres + selectinload expat + activité
    # groupée — CONSTANT quelle que soit la taille de la page.
    assert counter["n"] <= 6, f"directory list ran {counter['n']} queries"


async def test_profile_patch_mirrors_person_contract(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
) -> None:
    """Complément annuaire — PATCH de la fiche : écriture d'un champ civil
    et d'un custom person (forme PersonUpdateRequest), refus nommé d'un
    champ de portée dossier, refus cross-agence, dossiers intouchés
    (divergence visible à la lecture), tracé activity_log."""
    headers = agent_headers(admin)
    agency_id = admin.agency_id
    await _person_def(db_session, agency_id, "birth_country")
    case_def = CustomFieldDefinition(
        agency_id=agency_id, key="visa_type", label="visa_type", field_type="text", scope="case"
    )
    db_session.add(case_def)
    await db_session.commit()
    r = await client.post(
        "/cases",
        headers=headers,
        json={
            "first_name": "Patch",
            "last_name": "Fiche",
            "email": "patch-fiche@example.com",
            "nationality": "FR",
        },
    )
    assert r.status_code == 201, r.text
    case_id = r.json()["id"]
    listing = (await client.get("/client-profiles?search=patch-fiche", headers=headers)).json()
    profile_id = listing["items"][0]["id"]

    # ÉCRITURE : un civil (enum inclus) + un custom person, forme person.
    r = await client.patch(
        f"/client-profiles/{profile_id}",
        headers=headers,
        json={
            "nationality": "PT",
            "sex": "F",
            "preferred_channels": ["email", "email", "whatsapp"],
            "custom_fields": {"birth_country": "PT"},
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["nationality"] == "PT"
    assert body["sex"] == "F"
    assert body["preferred_channels"] == ["email", "whatsapp"]  # dédupliqué
    assert body["custom_fields"]["birth_country"] == "PT"
    # Le DOSSIER n'a pas bougé — sa divergence devient visible (F2.2).
    detail = (await client.get(f"/cases/{case_id}", headers=headers)).json()
    person = detail["persons"][0]
    assert person["nationality"] == "FR"
    assert person["differs_from_profile"] == ["nationality"]
    # Tracé sur le dossier vivant de la fiche.
    n_logs = (
        await db_session.execute(
            text("SELECT count(*) FROM activity_log WHERE action_type = 'profile.updated'")
        )
    ).scalar_one()
    assert n_logs == 1

    # REFUS : une clé custom de portée dossier → 422 nommé.
    r = await client.patch(
        f"/client-profiles/{profile_id}",
        headers=headers,
        json={"custom_fields": {"visa_type": "gold"}},
    )
    assert r.status_code == 422
    assert r.json()["code"] == "profile.reference_not_person_scope"
    # REFUS : un champ hors miroir (concept personne-au-dossier) → 422.
    r = await client.patch(
        f"/client-profiles/{profile_id}", headers=headers, json={"full_name": "X"}
    )
    assert r.status_code == 422

    # REFUS cross-agence : 404 non-révélateur.
    other = await make_agent(role=system_roles["admin"])
    r = await client.patch(
        f"/client-profiles/{profile_id}",
        headers=agent_headers(other),
        json={"nationality": "BR"},
    )
    assert r.status_code == 404


async def test_direct_profile_creation_dedup_and_deferred_linkage(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
) -> None:
    """Complément 2 (F4) — la fiche naît SANS compte (prospect à froid),
    l'email est dédupliqué 409 par agence, et la liaison se fait au PREMIER
    dossier (adoption par email) — par la « Nouvelle démarche » comme par
    un POST /cases ordinaire. Jamais deux fiches pour un même client."""
    headers = agent_headers(admin)
    # CRÉATION DIRECTE : identité + un civil du miroir.
    r = await client.post(
        "/client-profiles",
        headers=headers,
        json={
            "first_name": "Froid",
            "last_name": "Prospect",
            "email": "froid@example.com",
            "nationality": "AR",
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    profile_id = body["id"]
    assert body["expat_user_id"] is None  # AUCUN compte créé
    assert body["nationality"] == "AR"
    assert body["derived_status"] == "prospect"
    # Visible dans l'annuaire (recherche sur l'identité propre).
    listing = (await client.get("/client-profiles?search=froid@", headers=headers)).json()
    assert listing["total"] == 1
    assert listing["items"][0]["client_space_activated"] is False

    # DÉDUP : même email, même agence → 409 nommé (casse ignorée).
    r = await client.post(
        "/client-profiles",
        headers=headers,
        json={"first_name": "Bis", "last_name": "Bis", "email": "FROID@example.com"},
    )
    assert r.status_code == 409
    assert r.json()["code"] == "profile.email_taken"
    assert r.json()["params"]["profile_id"] == profile_id  # la RÉFÉRENCE (V1a)
    # Une AUTRE agence peut avoir le même email (annuaires distincts).
    other = await make_agent(role=system_roles["admin"])
    r = await client.post(
        "/client-profiles",
        headers=agent_headers(other),
        json={"first_name": "Autre", "last_name": "Agence", "email": "froid@example.com"},
    )
    assert r.status_code == 201, r.text

    # LIAISON DIFFÉRÉE chemin 1 — « Nouvelle démarche » depuis la fiche.
    r = await client.post(f"/client-profiles/{profile_id}/cases", headers=headers, json={})
    assert r.status_code in (200, 201), r.text
    case_id = r.json()["id"]
    detail = (await client.get(f"/client-profiles/{profile_id}", headers=headers)).json()
    assert detail["expat_user_id"] is not None  # ADOPTÉE, pas une 2e fiche
    assert len(detail["cases"]) == 1
    # L'ESPACE VIVANT (V1a) : le compte est né avec la démarche et son
    # invitation d'activation est partie — l'espace client existe.
    case_born = (await client.get(f"/cases/{case_id}", headers=headers)).json()
    principal = case_born["persons"][0]
    assert principal["expat_user_id"] == detail["expat_user_id"]
    assert principal["client_space_state"] is not None  # invitation vivante
    # Le prefill F2.1 a posé le civil de la fiche sur le dossier.
    case_detail = (await client.get(f"/cases/{case_id}", headers=headers)).json()
    assert case_detail["persons"][0]["nationality"] == "AR"
    assert case_detail["persons"][0]["client_profile_id"] == profile_id
    # L'annuaire ne compte toujours qu'UNE fiche pour cet email.
    listing = (await client.get("/client-profiles?search=froid@", headers=headers)).json()
    assert listing["total"] == 1

    # LIAISON DIFFÉRÉE chemin 2 — un POST /cases ordinaire adopte aussi.
    r = await client.post(
        "/client-profiles",
        headers=headers,
        json={"first_name": "Direct", "last_name": "Case", "email": "direct-case@example.com"},
    )
    assert r.status_code == 201, r.text
    second_profile_id = r.json()["id"]
    r = await client.post(
        "/cases",
        headers=headers,
        json={"first_name": "Direct", "last_name": "Case", "email": "direct-case@example.com"},
    )
    assert r.status_code == 201, r.text
    detail = (await client.get(f"/client-profiles/{second_profile_id}", headers=headers)).json()
    assert detail["expat_user_id"] is not None
    listing = (await client.get("/client-profiles?search=direct-case@", headers=headers)).json()
    assert listing["total"] == 1


# --- LOT SECTIONS : sections réelles sur la fiche + toggle scope ----------------------


async def test_profile_sections_follow_fiche_taxonomy(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
) -> None:
    """Lot taxonomie (ré-acté à la fusion id_documents → identity) — la
    fiche sert SES 4 sections (identity/contact/situation/misc), i18n en
    langue d'agence : les civils par le mapping code, les custom par
    leur colonne (défaut misc) ; dans identity, l'état civil d'abord,
    les documents ensuite (l'ordre du catalogue)."""
    headers = agent_headers(admin)
    await _person_def(db_session, admin.agency_id, "champ_maison")  # né misc
    await db_session.commit()

    r = await client.post(
        "/cases",
        headers=headers,
        json={"first_name": "Taxo", "last_name": "Fiche", "email": "taxo@example.com"},
    )
    assert r.status_code == 201, r.text
    listing = (await client.get("/client-profiles?search=taxo@", headers=headers)).json()
    detail = (
        await client.get(f"/client-profiles/{listing['items'][0]['id']}", headers=headers)
    ).json()
    sections = detail["sections"]
    by_key = {s["key"]: s for s in sections}
    # Les civils suivent le mapping code — id_documents n'existe plus,
    # le passeport vit dans identity, APRÈS l'état civil (ordre catalogue).
    assert "id_documents" not in by_key
    assert by_key["identity"]["references"] == [
        "date_of_birth",
        "nationality",
        "place_of_birth",
        "sex",
        "birth_name",
        "passport_number",
    ]
    assert "phone" in by_key["contact"]["references"]
    assert "profession" in by_key["situation"]["references"]
    # Le custom d'agence naît en Divers, reclassable par le toggle élargi.
    assert by_key["misc"]["references"] == ["champ_maison"]
    assert by_key["misc"]["name"] == "Divers"  # i18n langue d'agence (fr)


async def test_scope_toggle_reclassifies_definition(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
) -> None:
    """Lot sections point 2 — le TOGGLE scope : l'agence reclasse
    elle-même une définition 'case' en 'person' (elle apparaît sur la
    fiche et la complétude), et inversement. Les valeurs ne bougent pas."""
    headers = agent_headers(admin)
    r = await client.post(
        "/agencies/me/custom-fields",
        headers=headers,
        json={
            "key": "tax_residence_country",
            "label": "Pays de résidence fiscale",
            "field_type": "text",
        },
    )
    assert r.status_code == 201, r.text
    field_id = r.json()["id"]
    assert r.json()["scope"] == "case"  # naît 'case' (défaut prudent)

    r = await client.post(
        "/cases",
        headers=headers,
        json={"first_name": "Toggle", "last_name": "Scope", "email": "toggle@example.com"},
    )
    assert r.status_code == 201, r.text
    listing = (await client.get("/client-profiles?search=toggle@", headers=headers)).json()
    profile_id = listing["items"][0]["id"]
    completeness = (
        await client.get(f"/client-profiles/{profile_id}/completeness", headers=headers)
    ).json()
    assert "tax_residence_country" not in completeness["missing"]  # case : hors fiche

    # LE TOGGLE : case → person.
    r = await client.patch(
        f"/agencies/me/custom-fields/{field_id}", headers=headers, json={"scope": "person"}
    )
    assert r.status_code == 200, r.text
    assert r.json()["scope"] == "person"
    completeness = (
        await client.get(f"/client-profiles/{profile_id}/completeness", headers=headers)
    ).json()
    assert "tax_residence_country" in completeness["missing"]  # sur la fiche
    detail = (await client.get(f"/client-profiles/{profile_id}", headers=headers)).json()
    assert any("tax_residence_country" in s["references"] for s in detail["sections"])

    # Retour : person → case, la fiche l'oublie (les valeurs ne bougent pas).
    r = await client.patch(
        f"/agencies/me/custom-fields/{field_id}", headers=headers, json={"scope": "case"}
    )
    assert r.status_code == 200, r.text
    completeness = (
        await client.get(f"/client-profiles/{profile_id}/completeness", headers=headers)
    ).json()
    assert "tax_residence_country" not in completeness["missing"]

    # Valeur hors catalogue → 422 (Literal).
    r = await client.patch(
        f"/agencies/me/custom-fields/{field_id}", headers=headers, json={"scope": "global"}
    )
    assert r.status_code == 422

    # TOGGLE ÉLARGI (lot taxonomie) : reclasser la SECTION fiche — misc →
    # situation, la fiche suit ; hors taxonomie → 422.
    r = await client.patch(
        f"/agencies/me/custom-fields/{field_id}",
        headers=headers,
        json={"scope": "person", "profile_section": "situation"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["profile_section"] == "situation"
    detail = (await client.get(f"/client-profiles/{profile_id}", headers=headers)).json()
    by_key = {s["key"]: s["references"] for s in detail["sections"]}
    assert "tax_residence_country" in by_key["situation"]
    assert "tax_residence_country" not in by_key["misc"]
    r = await client.patch(
        f"/agencies/me/custom-fields/{field_id}",
        headers=headers,
        json={"profile_section": "rubrique_inconnue"},
    )
    assert r.status_code == 422


async def test_profile_notes_mirror_case_note_contract(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
) -> None:
    """Complément 2 — notes de fiche : mêmes formes que les notes de
    dossier (body, confidentialité, auteur, horodatage), même règle de
    confidentialité, seul l'auteur modifie/supprime."""
    headers = agent_headers(admin)
    r = await client.post(
        "/client-profiles",
        headers=headers,
        json={"first_name": "Note", "last_name": "Fiche", "email": "note-fiche@example.com"},
    )
    assert r.status_code == 201, r.text
    profile_id = r.json()["id"]

    # Créer : une normale + une confidentielle (admin a la permission).
    r = await client.post(
        f"/client-profiles/{profile_id}/notes", headers=headers, json={"body": "Vu au salon."}
    )
    assert r.status_code == 201, r.text
    note_id = r.json()["id"]
    assert r.json()["author_agent_id"] == str(admin.id)
    r = await client.post(
        f"/client-profiles/{profile_id}/notes",
        headers=headers,
        json={"body": "Budget sensible.", "is_confidential": True},
    )
    assert r.status_code == 201, r.text

    # La liste : l'admin voit 2 ; un viewer (sans note.view_confidential)
    # ne voit que la normale — même règle que le dossier.
    notes = (await client.get(f"/client-profiles/{profile_id}/notes", headers=headers)).json()
    assert len(notes) == 2
    viewer = await make_agent(agency_id=admin.agency_id, role=system_roles["viewer"])
    notes = (
        await client.get(f"/client-profiles/{profile_id}/notes", headers=agent_headers(viewer))
    ).json()
    assert [n["body"] for n in notes] == ["Vu au salon."]

    # Modifier : seul l'AUTEUR — un autre agent → 403 nommé.
    other = await make_agent(agency_id=admin.agency_id, role=system_roles["admin"])
    r = await client.patch(
        f"/client-profiles/{profile_id}/notes/{note_id}",
        headers=agent_headers(other),
        json={"body": "Piraté."},
    )
    assert r.status_code == 403
    assert r.json()["code"] == "profile.note_not_author"
    r = await client.patch(
        f"/client-profiles/{profile_id}/notes/{note_id}",
        headers=headers,
        json={"body": "Vu au salon, rappelé le 12."},
    )
    assert r.status_code == 200, r.text
    assert r.json()["body"] == "Vu au salon, rappelé le 12."

    # Supprimer : l'auteur, 204 ; la note disparaît.
    r = await client.delete(f"/client-profiles/{profile_id}/notes/{note_id}", headers=headers)
    assert r.status_code == 204
    notes = (await client.get(f"/client-profiles/{profile_id}/notes", headers=headers)).json()
    assert [n["body"] for n in notes] == ["Budget sensible."]

    # Cross-agence : 404 non-révélateur.
    stranger = await make_agent(role=system_roles["admin"])
    r = await client.get(f"/client-profiles/{profile_id}/notes", headers=agent_headers(stranger))
    assert r.status_code == 404


async def test_profile_activity_is_a_cross_read_of_case_logs(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
) -> None:
    """Complément 3 — le fil d'activité de la fiche : les activity_log de
    TOUS ses dossiers fusionnés antichronologiques, paginés, chaque
    entrée nommant son dossier d'origine ; les profile.updated y sont
    (loggés sur les dossiers). Aucun journal nouveau."""
    headers = agent_headers(admin)
    case_ids = []
    for _i in range(2):
        r = await client.post(
            "/cases",
            headers=headers,
            json={"first_name": "Fil", "last_name": "Activité", "email": "fil@example.com"},
        )
        assert r.status_code == 201, r.text
        case_ids.append(r.json()["id"])
    listing = (await client.get("/client-profiles?search=fil@", headers=headers)).json()
    profile_id = listing["items"][0]["id"]
    # Un PATCH fiche → profile.updated sur chaque dossier vivant.
    r = await client.patch(
        f"/client-profiles/{profile_id}", headers=headers, json={"nationality": "JP"}
    )
    assert r.status_code == 200, r.text

    feed = (await client.get(f"/client-profiles/{profile_id}/activity", headers=headers)).json()
    assert feed["total"] >= 2
    entries = feed["items"]
    # Antichronologique, et chaque entrée dit son dossier d'origine.
    times = [e["created_at"] for e in entries]
    assert times == sorted(times, reverse=True)
    assert {e["case_id"] for e in entries} == set(case_ids)  # les DEUX dossiers
    assert any(e["action_type"] == "profile.updated" for e in entries)
    # Paginé : page_size=1 → 1 entrée, même total.
    page1 = (
        await client.get(f"/client-profiles/{profile_id}/activity?page_size=1", headers=headers)
    ).json()
    assert len(page1["items"]) == 1
    assert page1["total"] == feed["total"]


async def test_every_person_field_has_exactly_one_profile_section(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
) -> None:
    """LA PIÈCE MAÎTRESSE (lot taxonomie) — l'ÉGALITÉ picker==fiche est
    REMPLACÉE par l'EXHAUSTIVITÉ : tout champ person a exactement UNE
    profile_section. Structurel (mappings code complets et valides) ET
    servi (l'union des sections == l'univers complétude, sans doublon,
    les 4 sections toujours là — deux univers picker/fiche assumés ;
    fusion id_documents → identity, parité société)."""
    from src.client_profiles.profile_sections import (
        CIVIL_PROFILE_SECTION,
        PRESET_PROFILE_SECTION,
        PROFILE_SECTIONS,
    )
    from src.progress.requirements_eval import COLLECTABLE_BASE_FIELDS

    # STRUCTUREL — les 10 civils couverts exactement, valeurs valides,
    # et LA liste exacte des 4 sections (l'univers, gravé).
    assert list(PROFILE_SECTIONS) == ["identity", "contact", "situation", "misc"]
    assert set(CIVIL_PROFILE_SECTION) == set(COLLECTABLE_BASE_FIELDS)
    assert set(CIVIL_PROFILE_SECTION.values()) <= set(PROFILE_SECTIONS)
    assert set(PRESET_PROFILE_SECTION.values()) <= set(PROFILE_SECTIONS)

    headers = agent_headers(admin)
    await _person_def(db_session, admin.agency_id, "clef_libre")
    await db_session.commit()
    r = await client.post(
        "/client-profiles",
        headers=headers,
        json={"first_name": "Exhau", "last_name": "Stif", "email": "exhaustif@example.com"},
    )
    assert r.status_code == 201, r.text
    detail = (await client.get(f"/client-profiles/{r.json()['id']}", headers=headers)).json()
    sections = detail["sections"]
    # Les 4 sections, l'ordre de la taxonomie, i18n fr — TOUJOURS servies.
    assert [s["key"] for s in sections] == list(PROFILE_SECTIONS.keys())
    # EXACTEMENT une section par champ : union == univers, zéro doublon.
    served = [ref for s in sections for ref in s["references"]]
    completeness = detail["completeness"]
    assert sorted(served) == sorted(completeness["filled"] + completeness["missing"])
    assert len(served) == len(set(served))


async def test_id_documents_merge_repoints_definitions(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
) -> None:
    """Fusion id_documents → identity (parité société) — LA fonction de
    migration sur données seedées : les défs de l'ancien monde
    re-pointent identity (comptées), idempotence (re-run = 0), le rendu
    les sert en identity (jamais en misc), et le toggle refuse
    l'ancienne valeur (422 — la taxonomie est à 4)."""
    from src.client_profiles.backfill import merge_person_id_documents_sections

    headers = agent_headers(admin)
    # Deux défs de l'ANCIEN monde (posées directement en DB, comme prod).
    for key in ("visa_type", "tax_id"):
        db_session.add(
            CustomFieldDefinition(
                agency_id=admin.agency_id,
                key=key,
                label=key,
                field_type="text",
                scope="person",
                profile_section="id_documents",
            )
        )
    await db_session.commit()

    stats = await db_session.run_sync(
        lambda sync_session: merge_person_id_documents_sections(sync_session.connection())
    )
    await db_session.commit()
    assert stats == {"definitions_repointed": 2}
    # IDEMPOTENCE : re-run = 0.
    stats2 = await db_session.run_sync(
        lambda sync_session: merge_person_id_documents_sections(sync_session.connection())
    )
    await db_session.commit()
    assert stats2 == {"definitions_repointed": 0}

    # Le rendu les sert en identity, APRÈS l'état civil (ordre catalogue).
    r = await client.post(
        "/client-profiles",
        headers=headers,
        json={"first_name": "Fusion", "last_name": "Docs", "email": "fusion-docs@example.com"},
    )
    assert r.status_code == 201, r.text
    detail = (await client.get(f"/client-profiles/{r.json()['id']}", headers=headers)).json()
    by_key = {s["key"]: s for s in detail["sections"]}
    assert "id_documents" not in by_key
    identity = by_key["identity"]["references"]
    assert identity.index("visa_type") > identity.index("sex")
    assert identity.index("tax_id") > identity.index("visa_type")  # l'ordre du catalogue
    assert "visa_type" not in by_key["misc"]["references"]

    # Le toggle agence : mécanique INCHANGÉE, l'ancienne valeur refusée.
    definitions = (await client.get("/agencies/me/custom-fields", headers=headers)).json()
    visa_def = next(d for d in definitions if d["key"] == "visa_type")
    r = await client.patch(
        f"/agencies/me/custom-fields/{visa_def['id']}",
        headers=headers,
        json={"profile_section": "id_documents"},
    )
    assert r.status_code == 422
    r = await client.patch(
        f"/agencies/me/custom-fields/{visa_def['id']}",
        headers=headers,
        json={"profile_section": "situation"},
    )
    assert r.status_code == 200, r.text


# --- MÉGA-LOT SOLDE CRM ---------------------------------------------------------------


async def test_status_override_primes_over_derivation(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
) -> None:
    """V1b + règle actée — dossier vivant → client par dérivation ; SEUL
    l'override explicite de l'agence peut afficher Prospect ; null rend
    la main ; le FILTRE suit l'override ; le geste est tracé."""
    headers = agent_headers(admin)
    r = await client.post(
        "/cases",
        headers=headers,
        json={"first_name": "Over", "last_name": "Ride", "email": "override@example.com"},
    )
    assert r.status_code == 201, r.text
    listing = (await client.get("/client-profiles?search=override@", headers=headers)).json()
    item = listing["items"][0]
    assert item["derived_status"] == "client"  # dossier vivant → client
    profile_id = item["id"]

    # L'OVERRIDE force Prospect malgré le dossier vivant (le seul chemin).
    r = await client.patch(
        f"/client-profiles/{profile_id}", headers=headers, json={"status_override": "prospect"}
    )
    assert r.status_code == 200, r.text
    assert r.json()["derived_status"] == "prospect"
    assert r.json()["status_override"] == "prospect"
    filtered = (
        await client.get("/client-profiles?search=override@&status=prospect", headers=headers)
    ).json()
    assert filtered["total"] == 1
    filtered = (
        await client.get("/client-profiles?search=override@&status=client", headers=headers)
    ).json()
    assert filtered["total"] == 0
    # Tracé.
    n_logs = (
        await db_session.execute(
            text(
                "SELECT count(*) FROM activity_log WHERE action_type = 'profile.updated' "
                "AND details->'fields' ? 'status_override'"
            )
        )
    ).scalar_one()
    assert n_logs >= 1
    # Null explicite → retour à la dérivation (client).
    r = await client.patch(
        f"/client-profiles/{profile_id}", headers=headers, json={"status_override": None}
    )
    assert r.status_code == 200, r.text
    assert r.json()["derived_status"] == "client"
    assert r.json()["status_override"] is None


async def test_relationship_kind_enumerated(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
) -> None:
    """V2a — le rôle canonique énuméré à côté du libellé libre, posé à la
    création, éditable au PATCH, hors catalogue → 422."""
    headers = agent_headers(admin)
    r = await client.post(
        "/cases",
        headers=headers,
        json={"first_name": "Rel", "last_name": "Kind", "email": "relkind@example.com"},
    )
    assert r.status_code == 201, r.text
    case_id = r.json()["id"]
    r = await client.post(
        f"/cases/{case_id}/persons",
        headers=headers,
        json={"full_name": "Marie K", "relationship": "Épouse", "relationship_kind": "spouse"},
    )
    assert r.status_code == 201, r.text
    person = r.json()
    assert person["relationship"] == "Épouse"  # le libellé LIBRE reste
    assert person["relationship_kind"] == "spouse"
    r = await client.patch(
        f"/cases/{case_id}/persons/{person['id']}",
        headers=headers,
        json={"relationship_kind": "partner"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["relationship_kind"] == "partner"
    r = await client.post(
        f"/cases/{case_id}/persons",
        headers=headers,
        json={"full_name": "X", "relationship": "x", "relationship_kind": "belle_mere"},
    )
    assert r.status_code == 422


async def test_directory_filters_and_sorts(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
) -> None:
    """V3 — les filtres (tags, espace activé, has_active_case) et les
    tris SQL (pagination juste), au MÊME coût constant (témoin)."""
    from sqlalchemy import event

    from src.client_profiles.client_profiles_manager import ClientProfilesManager

    headers = agent_headers(admin)
    for i, email_addr in enumerate(("f1@ex.com", "f2@ex.com")):
        r = await client.post(
            "/cases",
            headers=headers,
            json={"first_name": f"Filt{i}", "last_name": "Annuaire", "email": email_addr},
        )
        assert r.status_code == 201, r.text
        if i == 0:
            # f1 : tagué + dossier clos (pas de dossier actif).
            listing = (await client.get("/client-profiles?search=f1@", headers=headers)).json()
            await client.patch(
                f"/client-profiles/{listing['items'][0]['id']}",
                headers=headers,
                json={},
            )
            await db_session.execute(
                text("UPDATE client_profile SET tags = '[\"vip\"]' WHERE id = :i"),
                {"i": listing["items"][0]["id"]},
            )
            await db_session.execute(
                text("UPDATE client_case SET status = 'closed' WHERE id = :c"),
                {"c": r.json()["id"]},
            )
            await db_session.commit()
    tagged = (await client.get("/client-profiles?tags=vip&search=@ex.com", headers=headers)).json()
    assert [i["email"] for i in tagged["items"]] == ["f1@ex.com"]
    actives = (
        await client.get("/client-profiles?has_active_case=true&search=@ex.com", headers=headers)
    ).json()
    assert [i["email"] for i in actives["items"]] == ["f2@ex.com"]
    inactives = (
        await client.get("/client-profiles?has_active_case=false&search=@ex.com", headers=headers)
    ).json()
    assert [i["email"] for i in inactives["items"]] == ["f1@ex.com"]
    not_activated = (
        await client.get(
            "/client-profiles?client_space_activated=false&search=@ex.com", headers=headers
        )
    ).json()
    assert not_activated["total"] == 2  # personne n'a activé
    by_created = (
        await client.get(
            "/client-profiles?sort_by=created_at&sort_order=desc&search=@ex.com", headers=headers
        )
    ).json()
    assert [i["email"] for i in by_created["items"]] == ["f2@ex.com", "f1@ex.com"]
    # Témoin : filtres + tri dernière activité, coût toujours constant.
    engine = db_session.get_bind()
    counter = {"n": 0}

    def _count(*_args: object, **_kwargs: object) -> None:
        counter["n"] += 1

    event.listen(engine, "before_cursor_execute", _count)
    try:
        await ClientProfilesManager(db_session).list_profiles(
            admin,
            search=None,
            status=None,
            tags=["vip"],
            has_active_case=True,
            sort_by="last_activity",
            sort_order="desc",
            page=1,
            page_size=20,
        )
    finally:
        event.remove(engine, "before_cursor_execute", _count)
    assert counter["n"] <= 6, f"directory ran {counter['n']} queries"


async def test_patch_language_and_email_rules(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
) -> None:
    """Complément PATCH — la langue (registre d'agence, prime compte) ;
    l'email : libre → OK + dédup 409 avec référence ; lié → 422 nommé."""
    headers = agent_headers(admin)
    # Fiche LIBRE : langue + email éditables.
    r = await client.post(
        "/client-profiles",
        headers=headers,
        json={"first_name": "Libre", "last_name": "Fiche", "email": "libre@example.com"},
    )
    assert r.status_code == 201, r.text
    free_id = r.json()["id"]
    r = await client.patch(
        f"/client-profiles/{free_id}",
        headers=headers,
        json={"preferred_lang": "es", "email": "libre2@example.com"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["preferred_lang"] == "es"
    assert r.json()["email"] == "libre2@example.com"
    # Langue hors catalogue → 422 (Literal).
    r = await client.patch(
        f"/client-profiles/{free_id}", headers=headers, json={"preferred_lang": "de"}
    )
    assert r.status_code == 422

    # Fiche LIÉE (créée par un dossier) : email verrouillé, langue OK.
    r = await client.post(
        "/cases",
        headers=headers,
        json={"first_name": "Lié", "last_name": "Compte", "email": "lie@example.com"},
    )
    assert r.status_code == 201, r.text
    listing = (await client.get("/client-profiles?search=lie@", headers=headers)).json()
    linked_id = listing["items"][0]["id"]
    r = await client.patch(
        f"/client-profiles/{linked_id}", headers=headers, json={"email": "autre@example.com"}
    )
    assert r.status_code == 422
    assert r.json()["code"] == "profile.email_locked_by_account"
    r = await client.patch(
        f"/client-profiles/{linked_id}", headers=headers, json={"preferred_lang": "ru"}
    )
    assert r.status_code == 200, r.text
    assert r.json()["preferred_lang"] == "ru"  # la fiche prime sur le compte (fr)

    # DÉDUP à jour : l'email d'une autre fiche de l'agence → 409 + référence.
    r = await client.patch(
        f"/client-profiles/{free_id}", headers=headers, json={"email": "lie@example.com"}
    )
    assert r.status_code == 409
    assert r.json()["code"] == "profile.email_taken"
    assert r.json()["params"]["profile_id"] == linked_id
    # Re-poser SON propre email : no-op propre.
    r = await client.patch(
        f"/client-profiles/{free_id}", headers=headers, json={"email": "LIBRE2@example.com"}
    )
    assert r.status_code == 200, r.text


async def test_direct_creation_without_email(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
) -> None:
    """Dernier complément — l'email est OPTIONNEL au POST : le prospect à
    froid sans email existe (deux sans-email coexistent — rien à
    dédupliquer), « Nouvelle démarche » 422 nommé tant que l'email n'est
    pas posé au PATCH, puis tout se débloque."""
    headers = agent_headers(admin)
    r = await client.post(
        "/client-profiles",
        headers=headers,
        json={"first_name": "Sans", "last_name": "Email"},
    )
    assert r.status_code == 201, r.text
    profile_id = r.json()["id"]
    assert r.json()["email"] == ""  # servi vide, pas d'invention
    # Un DEUXIÈME sans-email coexiste (pas de fausse dédup sur le vide).
    r = await client.post(
        "/client-profiles",
        headers=headers,
        json={"first_name": "Autre", "last_name": "SansEmail"},
    )
    assert r.status_code == 201, r.text
    # « Nouvelle démarche » impossible sans email — 422 nommé.
    r = await client.post(f"/client-profiles/{profile_id}/cases", headers=headers, json={})
    assert r.status_code == 422
    assert r.json()["code"] == "profile.no_email"
    # L'email arrive au PATCH → la démarche se débloque.
    r = await client.patch(
        f"/client-profiles/{profile_id}", headers=headers, json={"email": "tardif@example.com"}
    )
    assert r.status_code == 200, r.text
    r = await client.post(f"/client-profiles/{profile_id}/cases", headers=headers, json={})
    assert r.status_code in (200, 201), r.text


async def test_patch_tags_for_bulk_actions(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
) -> None:
    """Actions groupées (2b) — tags au PATCH fiche : état REMPLACÉ,
    dédupliqué, servi en liste (le filtre tags= le voit aussitôt)."""
    headers = agent_headers(admin)
    r = await client.post(
        "/client-profiles",
        headers=headers,
        json={"first_name": "Tag", "last_name": "Groupé", "email": "tags@example.com"},
    )
    assert r.status_code == 201, r.text
    profile_id = r.json()["id"]
    r = await client.patch(
        f"/client-profiles/{profile_id}",
        headers=headers,
        json={"tags": ["vip", "salon-2026", "vip"]},
    )
    assert r.status_code == 200, r.text
    assert r.json()["tags"] == ["vip", "salon-2026"]  # dédupliqué, ordre gardé
    listing = (await client.get("/client-profiles?tags=salon-2026", headers=headers)).json()
    assert [i["id"] for i in listing["items"]] == [profile_id]


async def test_living_case_never_shows_prospect_without_explicit_override(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
) -> None:
    """Constat capture — une fiche avec dossier VIVANT ne peut JAMAIS
    s'afficher Prospect (liste ET détail), même si le dossier est en
    statut 'prospect' ; seul l'override explicite le peut, et ni le
    backfill ni l'import n'en posent."""
    headers = agent_headers(admin)
    # Le scénario exact de la capture : dossier créé, resté en 'prospect'.
    r = await client.post(
        "/cases",
        headers=headers,
        json={"first_name": "Ana", "last_name": "Costa", "email": "ana-costa@example.com"},
    )
    assert r.status_code == 201, r.text
    await db_session.execute(
        text("UPDATE client_case SET status = 'prospect' WHERE id = :i"), {"i": r.json()["id"]}
    )
    await db_session.commit()
    listing = (await client.get("/client-profiles?search=ana-costa@", headers=headers)).json()
    item = listing["items"][0]
    assert item["cases_count"] == 1
    assert item["derived_status"] == "client"  # JAMAIS Prospect avec un dossier vivant
    detail = (await client.get(f"/client-profiles/{item['id']}", headers=headers)).json()
    assert detail["derived_status"] == "client"  # liste == détail
    assert detail["status_override"] is None  # aucun override posé par la création
    # L'import n'en pose pas non plus.
    r = await client.post(
        "/imports/client-profiles",
        headers=headers,
        json={
            "csv_text": "Prénom,Nom,Email\nSans,Dossier,import-p@example.com\n",
            "mapping": {"Prénom": "first_name", "Nom": "last_name", "Email": "email"},
        },
    )
    assert r.status_code == 200, r.text
    imported = (await client.get("/client-profiles?search=import-p@", headers=headers)).json()
    assert imported["items"][0]["derived_status"] == "prospect"  # sans dossier
    assert imported["items"][0]["status_override"] is None


async def test_profile_serves_its_companies_reverse_link(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
) -> None:
    """Complément garde — « Ses sociétés » : la lecture inverse du lien.
    Deux sociétés → les deux servies avec rôles ; zéro → [] ; le DELETE
    existant côté société est appelable avec les ids servis."""
    headers = agent_headers(admin)
    r = await client.post(
        "/client-profiles",
        headers=headers,
        json={"first_name": "Multi", "last_name": "Sociétés", "email": "multi-soc@example.com"},
    )
    assert r.status_code == 201, r.text
    person_id = r.json()["id"]
    assert r.json()["companies"] == []  # zéro → []
    company_ids = []
    for name, role in (("Alpha SL", "manager"), ("Beta GmbH", "partner")):
        r = await client.post("/company-profiles", headers=headers, json={"name": name})
        assert r.status_code == 201, r.text
        company_ids.append(r.json()["id"])
        r = await client.post(
            f"/company-profiles/{company_ids[-1]}/roles",
            headers=headers,
            json={"client_profile_id": person_id, "role": role},
        )
        assert r.status_code == 201, r.text
    detail = (await client.get(f"/client-profiles/{person_id}", headers=headers)).json()
    companies = detail["companies"]
    assert [(c["name"], c["role"]) for c in companies] == [
        ("Alpha SL", "manager"),
        ("Beta GmbH", "partner"),
    ]
    # La DISSOCIATION côté personne : le DELETE société existant, avec les
    # ids TELS QUE SERVIS (constat point 2 — appelable, une ligne).
    first = companies[0]
    r = await client.delete(
        f"/company-profiles/{first['company_id']}/roles/{first['role_id']}", headers=headers
    )
    assert r.status_code == 204
    detail = (await client.get(f"/client-profiles/{person_id}", headers=headers)).json()
    assert [c["name"] for c in detail["companies"]] == ["Beta GmbH"]


async def test_delete_profile_rules(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
) -> None:
    """Suppression unitaire — fiche libre : 204 + cascade (notes, rôles) ;
    fiche avec dossier (même clos) : 409 avec le compte ; fiche liée à un
    compte sans dossier : 204, le compte global SURVIT intouché."""
    headers = agent_headers(admin)
    # 1. Fiche libre avec note + rôle société → 204, tout part en cascade.
    r = await client.post(
        "/client-profiles",
        headers=headers,
        json={"first_name": "À", "last_name": "Supprimer", "email": "delete-me@example.com"},
    )
    assert r.status_code == 201, r.text
    free_id = r.json()["id"]
    r = await client.post(
        f"/client-profiles/{free_id}/notes", headers=headers, json={"body": "note orpheline"}
    )
    assert r.status_code == 201, r.text
    r = await client.post("/company-profiles", headers=headers, json={"name": "CascadeCo"})
    company_id = r.json()["id"]
    r = await client.post(
        f"/company-profiles/{company_id}/roles",
        headers=headers,
        json={"client_profile_id": free_id, "role": "contact"},
    )
    assert r.status_code == 201, r.text
    r = await client.delete(f"/client-profiles/{free_id}", headers=headers)
    assert r.status_code == 204
    assert (await client.get(f"/client-profiles/{free_id}", headers=headers)).status_code == 404
    n_notes = (
        await db_session.execute(text("SELECT count(*) FROM client_profile_note"))
    ).scalar_one()
    n_roles = (
        await db_session.execute(text("SELECT count(*) FROM company_profile_role"))
    ).scalar_one()
    assert (n_notes, n_roles) == (0, 0)  # cascade propre

    # 2. Fiche avec dossier (passé CLOS — l'historique est sacré) → 409.
    r = await client.post(
        "/cases",
        headers=headers,
        json={"first_name": "Avec", "last_name": "Dossier", "email": "has-case@example.com"},
    )
    assert r.status_code == 201, r.text
    await db_session.execute(
        text("UPDATE client_case SET status = 'closed' WHERE id = :i"), {"i": r.json()["id"]}
    )
    await db_session.commit()
    listing = (await client.get("/client-profiles?search=has-case@", headers=headers)).json()
    protected_id = listing["items"][0]["id"]
    r = await client.delete(f"/client-profiles/{protected_id}", headers=headers)
    assert r.status_code == 409
    assert r.json()["code"] == "profile.has_cases"
    assert r.json()["params"]["cases_count"] == 1

    # 3. Fiche LIÉE à un compte, zéro dossier → 204, le compte survit.
    expat = await make_expat_user(activated=True, email="compte-survit@example.com")
    r = await client.post(
        "/client-profiles",
        headers=headers,
        json={"first_name": "Compte", "last_name": "Survit", "email": "compte-survit@example.com"},
    )
    assert r.status_code == 201, r.text
    linked_id = r.json()["id"]
    await db_session.execute(
        text("UPDATE client_profile SET expat_user_id = :e WHERE id = :i"),
        {"e": expat.id, "i": linked_id},
    )
    await db_session.commit()
    r = await client.delete(f"/client-profiles/{linked_id}", headers=headers)
    assert r.status_code == 204
    n_accounts = (
        await db_session.execute(
            text("SELECT count(*) FROM expat_user WHERE email = 'compte-survit@example.com'")
        )
    ).scalar_one()
    assert n_accounts == 1  # le compte global est INTOUCHÉ


async def test_auto_promotion_on_person_write(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
) -> None:
    """Écriture d'un champ person sur une personne LIÉE : fiche vide pour
    ce champ → promotion AUTOMATIQUE tracée ; fiche différente → rien (la
    divergence reste) — et la réponse d'ÉCRITURE dit la vérité
    (differs_from_profile servi, le trou du badge est bouché)."""
    headers = agent_headers(admin)
    await _person_def(db_session, admin.agency_id, "birth_country")
    await db_session.commit()
    # Création AVEC valeurs : auto-promotion dès la naissance (fiche vide).
    r = await client.post(
        "/cases",
        headers=headers,
        json={
            "first_name": "Auto",
            "last_name": "Promo",
            "email": "auto-promo@example.com",
            "nationality": "FR",
            "custom_fields": {"birth_country": "FR"},
        },
    )
    assert r.status_code == 201, r.text
    case_id = r.json()["id"]
    listing = (await client.get("/client-profiles?search=auto-promo@", headers=headers)).json()
    profile_id = listing["items"][0]["id"]
    detail = (await client.get(f"/client-profiles/{profile_id}", headers=headers)).json()
    assert detail["nationality"] == "FR"  # promue à la création
    assert detail["custom_fields"]["birth_country"] == "FR"
    n_auto = (
        await db_session.execute(
            text(
                "SELECT count(*) FROM activity_log WHERE action_type = 'profile.field_promoted' "
                "AND (details->>'auto')::boolean"
            )
        )
    ).scalar_one()
    assert n_auto == 2  # nationality + birth_country, tracées depuis le dossier

    # ÉCRITURE d'un champ que la fiche n'a pas → promotion auto.
    case_detail = (await client.get(f"/cases/{case_id}", headers=headers)).json()
    person = case_detail["persons"][0]
    r = await client.patch(
        f"/cases/{case_id}/persons/{person['id']}",
        headers=headers,
        json={"profession": "Notaire"},
    )
    assert r.status_code == 200, r.text
    detail = (await client.get(f"/client-profiles/{profile_id}", headers=headers)).json()
    assert detail["profession"] == "Notaire"
    # ÉCRITURE d'une valeur DIFFÉRENTE de la fiche → rien ne bouge, et la
    # RÉPONSE DU PATCH sert la divergence (le badge d'Alexandre).
    r = await client.patch(
        f"/cases/{case_id}/persons/{person['id']}",
        headers=headers,
        json={"nationality": "PT"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["differs_from_profile"] == ["nationality"]  # la réponse dit VRAI
    detail = (await client.get(f"/client-profiles/{profile_id}", headers=headers)).json()
    assert detail["nationality"] == "FR"  # la fiche n'a pas bougé


async def test_inherited_keys_marker_lifecycle(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    expat_headers: AuthHeaders,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
) -> None:
    """Option B — le marqueur hérité/saisi : POSÉ au prefill (les valeurs
    venues de la fiche), SERVI au contrat person, EFFACÉ à toute écriture
    — agence (PATCH person) OU client (fulfill d'exigence)."""
    headers = agent_headers(admin)
    expat = await make_expat_user(activated=True, email="inherit@example.com")
    # La fiche porte nationality + profession AVANT le dossier.
    r = await client.post(
        "/client-profiles",
        headers=headers,
        json={
            "first_name": "Inh",
            "last_name": "Erit",
            "email": "inherit@example.com",
            "nationality": "FR",
            "profession": "Notaire",
        },
    )
    assert r.status_code == 201, r.text
    profile_id = r.json()["id"]
    # Parcours avec exigence base_field nationality (le chemin CLIENT).
    tid = (await client.post("/journeys", headers=headers, json={"name": "T"})).json()["id"]
    sid = (
        await client.post(f"/journeys/{tid}/steps", headers=headers, json={"name": "Collecte"})
    ).json()["id"]
    await client.post(
        f"/journeys/{tid}/fields",
        headers=headers,
        json={"kind": "base_field", "reference": "nationality"},
    )
    r = await client.post(
        f"/journeys/{tid}/steps/{sid}/requirements",
        headers=headers,
        json={"kind": "base_field", "reference": "nationality", "scope": "principal"},
    )
    assert r.status_code == 201, r.text

    # « Nouvelle démarche » : le prefill pose les valeurs ET le marqueur.
    r = await client.post(f"/client-profiles/{profile_id}/cases", headers=headers, json={})
    assert r.status_code in (200, 201), r.text
    case_id = r.json()["id"]
    detail = (await client.get(f"/cases/{case_id}", headers=headers)).json()
    person = detail["persons"][0]
    assert person["nationality"] == "FR"
    assert sorted(person["inherited_keys"]) == ["nationality", "profession"]  # SERVI

    # Écriture AGENCE : profession saisie (même valeur !) → mention retirée.
    r = await client.patch(
        f"/cases/{case_id}/persons/{person['id']}",
        headers=headers,
        json={"profession": "Notaire"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["inherited_keys"] == ["nationality"]

    # Écriture CLIENT : le fulfill de nationality → mention retirée aussi.
    steps = (
        await client.post(
            f"/cases/{case_id}/journey", headers=headers, json={"journey_template_id": tid}
        )
    ).json()
    r = await client.patch(
        f"/cases/{case_id}/steps/{steps[0]['id']}", headers=headers, json={"status": "in_progress"}
    )
    assert r.status_code == 200, r.text
    exp_detail = (await client.get(f"/expat/cases/{case_id}", headers=expat_headers(expat))).json()
    req = next(
        r
        for step in exp_detail["timeline"]
        for r in step["requirements"]
        if r["reference"] == "nationality"
    )
    r = await client.put(
        f"/expat/cases/{case_id}/requirements/{req['id']}",
        headers=expat_headers(expat),
        json={"value": "PT"},
    )
    assert r.status_code == 200, r.text
    detail = (await client.get(f"/cases/{case_id}", headers=headers)).json()
    person = detail["persons"][0]
    assert person["nationality"] == "PT"
    assert person["inherited_keys"] == []  # le client a saisi : plus d'hérité

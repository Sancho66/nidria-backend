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
    assert item["derived_status"] == "prospect"  # dossier né prospect
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
    # Prospect pur (dossier né prospect) + client (dossier avancé).
    for i, email_addr in enumerate(("annuaire-p@example.com", "annuaire-c@example.com")):
        r = await client.post(
            "/cases",
            headers=headers,
            json={"first_name": f"Annuaire{i}", "last_name": "Test", "email": email_addr},
        )
        assert r.status_code == 201, r.text
        if email_addr.startswith("annuaire-c"):
            await db_session.execute(
                text("UPDATE client_case SET status = 'in_progress' WHERE id = :i"),
                {"i": r.json()["id"]},
            )
            await db_session.commit()
    all_items = (await client.get("/client-profiles?search=annuaire-", headers=headers)).json()
    assert all_items["total"] == 2
    for item in all_items["items"]:
        assert isinstance(item["tags"], list)
        assert item["last_activity_at"] is not None  # activity_log du dossier
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


async def test_profile_sections_follow_real_referential_sections(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
) -> None:
    """CORRECTION (complément) — la fiche groupe par la CATÉGORIE
    EXISTANTE du catalogue (le rail du picker) : rien à inventer.
    birth_country vit en Identité au catalogue ; une clé d'agence hors
    catalogue tombe dans « Sans catégorie », TOUJOURS en dernier."""
    headers = agent_headers(admin)
    await _person_def(db_session, admin.agency_id, "birth_country")
    await _person_def(db_session, admin.agency_id, "champ_maison")  # hors catalogue
    await db_session.commit()

    r = await client.post(
        "/cases",
        headers=headers,
        json={"first_name": "Sect", "last_name": "Ion", "email": "sections@example.com"},
    )
    assert r.status_code == 201, r.text
    listing = (await client.get("/client-profiles?search=sections@", headers=headers)).json()
    detail = (
        await client.get(f"/client-profiles/{listing['items'][0]['id']}", headers=headers)
    ).json()
    sections = detail["sections"]
    by_name = {s["name"]: s["references"] for s in sections}
    # Les catégories du catalogue, dans SA langue (agence fr) et SON ordre.
    assert "nationality" in by_name["Identité"]
    assert "birth_country" in by_name["Identité"]
    assert "phone" in by_name["Contact"]
    # La clé d'agence hors catalogue → panier final « Sans catégorie »
    # (avec les colonnes civiles que le rail du picker ne référence pas,
    # ex. birth_name — la vérité du catalogue, pas un bug).
    assert sections[-1]["name"] is None
    assert "champ_maison" in sections[-1]["references"]
    # L'univers des sections == l'univers de la complétude (rien de perdu).
    completeness = detail["completeness"]
    in_sections = [ref for s in sections for ref in s["references"]]
    assert sorted(in_sections) == sorted(completeness["filled"] + completeness["missing"])


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

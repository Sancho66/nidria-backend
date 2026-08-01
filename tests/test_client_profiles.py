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

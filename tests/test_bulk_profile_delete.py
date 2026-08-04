"""La SUPPRESSION DE MASSE de fiches — par sélection ou par critère.

Quatre lois, gravées ici :

1. **« Tout ce que je vois » == « tout ce que je supprime ».** Le filtre
   de suppression est celui de la liste, appliqué par les MÊMES prédicats
   SQL. Un témoin structurel vérifie que les deux déclarations ne peuvent
   pas diverger — c'est la classe de bug qu'on interdit de renaître.
2. **La protection ne bouge pas d'un pouce.** Une fiche qu'un dossier
   référence — vivant, clos ou supprimé — n'est jamais supprimée. À
   l'unité c'était un 409 ; en masse c'est un compte agrégé. Même règle,
   autre forme.
3. **Le compte annoncé EST le compte réel.** `dry_run` et exécution
   passent par le même chemin ; la comparaison est faite ici sur la même
   base, dans le même test.
4. **Jamais au-delà de l'agence.** Ni par filtre, ni par identifiants
   empruntés à la voisine.
"""

import uuid
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from shared.models.bulk_deletion import BulkDeletionLog
from shared.models.client_profile import ClientProfile
from shared.models.company_profile import CompanyProfile
from shared.models.rbac import Role
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent
from tests.plugins.case_plugin import MakeClientCase
from tests.plugins.expat_plugin import MakeExpatUser

pytestmark = pytest.mark.usefixtures("rbac_baseline")


@pytest_asyncio.fixture
async def admin(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    return await make_agent(role=system_roles["admin"])


async def _free_profile(db: AsyncSession, agency_id: uuid.UUID, **overrides: object) -> uuid.UUID:
    """Une fiche LIBRE : sans compte ni dossier — la seule espèce qu'on
    ait le droit de supprimer. Posée en base plutôt que par l'API : les
    `tags` (un critère de la liste, donc de la suppression) ne sont pas
    dans le contrat de création, ils naissent de l'import ou d'un PATCH."""
    fields: dict[str, object] = {
        "first_name": "Libre",
        "last_name": "Sansdossier",
        "email": f"libre-{uuid.uuid4().hex[:8]}@example.com",
    }
    fields.update(overrides)
    profile = ClientProfile(agency_id=agency_id, **fields)  # type: ignore[arg-type]
    db.add(profile)
    await db.commit()
    return profile.id


async def _bulk(client: AsyncClient, headers: dict[str, str], **body: object) -> dict:
    response = await client.post("/client-profiles/bulk-delete", headers=headers, json=body)
    assert response.status_code == 200, response.text
    return response.json()


# ─── 1. Le filtre de la liste EST celui de la suppression ─────────────────


def test_delete_filter_declares_exactly_the_list_filters() -> None:
    """LE TÉMOIN ANTI-DÉRIVE, structurel et sans base.

    Si un critère entre dans la liste sans entrer dans le filtre de
    suppression, l'agence supprimerait PLUS LARGE que ce qu'elle voit —
    en silence, et sans moyen de s'en apercevoir avant les dégâts. Ce
    test compare les deux déclarations, champ par champ.
    """
    import inspect

    from src.client_profiles.client_profiles_router import list_client_profiles
    from src.client_profiles.client_profiles_schema import ProfileListFilter
    from src.company_profiles.company_profiles_router import list_company_profiles
    from src.company_profiles.company_profiles_schema import CompanyListFilter

    # Les paramètres de la liste qui NE filtrent pas : ils choisissent
    # l'ordre et la tranche montrée, jamais QUI est visé.
    not_filters = {"agent", "db", "sort_by", "sort_order", "page", "page_size", "lang"}

    for endpoint, model, face in (
        (list_client_profiles, ProfileListFilter, "personne"),
        (list_company_profiles, CompanyListFilter, "société"),
    ):
        listed = set(inspect.signature(endpoint).parameters) - not_filters
        declared = set(model.model_fields)
        assert listed == declared, (
            f"face {face} — la liste et la suppression ont divergé : "
            f"liste seule {sorted(listed - declared)}, "
            f"suppression seule {sorted(declared - listed)}"
        )


async def test_filter_deletes_exactly_what_the_list_shows(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
) -> None:
    """La preuve à l'exécution : ce que la liste compte sous un critère
    est ce que la suppression vise sous le même critère."""
    headers = agent_headers(admin)
    for index in range(4):
        await _free_profile(
            db_session,
            admin.agency_id,
            last_name="Cible" if index < 3 else "Épargnée",
            tags=["a-purger"] if index < 3 else ["à-garder"],
        )

    criteria = {"tags": ["a-purger"]}
    listed = await client.get("/client-profiles", headers=headers, params=criteria)
    assert listed.status_code == 200, listed.text
    shown = listed.json()["total"]
    assert shown == 3

    report = await _bulk(client, headers, filter=criteria, dry_run=True)
    assert report["matching"] == shown, "le filtre ne vise pas ce que la liste montre"
    assert report["deletable"] == shown

    report = await _bulk(client, headers, filter=criteria)
    assert report["deleted"] == 3
    # L'épargnée est toujours là — et elle seule.
    remaining = await client.get("/client-profiles", headers=headers)
    assert remaining.json()["total"] == 1
    assert remaining.json()["items"][0]["last_name"] == "Épargnée"


async def test_empty_filter_means_the_whole_agency(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
) -> None:
    """`filter: {}` = une liste sans filtre = toutes les fiches. Légitime,
    mais EXPLICITE : l'omission des deux sélecteurs est refusée."""
    headers = agent_headers(admin)
    for _ in range(3):
        await _free_profile(db_session, admin.agency_id)

    report = await _bulk(client, headers, filter={}, dry_run=True)
    assert report["matching"] == 3

    # Ni `ids` ni `filter` → 422. On ne supprime pas tout par omission.
    response = await client.post("/client-profiles/bulk-delete", headers=headers, json={})
    assert response.status_code == 422
    # Les deux à la fois → 422 aussi : le geste doit être sans ambiguïté.
    response = await client.post(
        "/client-profiles/bulk-delete",
        headers=headers,
        json={"ids": [str(uuid.uuid4())], "filter": {}},
    )
    assert response.status_code == 422


# ─── 2. La protection, en masse ───────────────────────────────────────────


async def test_protection_holds_in_bulk_mixed_selection(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
) -> None:
    """MÉLANGE RÉEL : des fiches libres et des fiches à dossier, dans la
    même charge. Les libres partent, les autres restent — et le rapport
    les NOMME plutôt que de les taire."""
    headers = agent_headers(admin)
    free_ids = [await _free_profile(db_session, admin.agency_id) for _ in range(3)]

    # Trois fiches protégées, une par état de dossier : VIVANT, CLOS,
    # SUPPRIMÉ. L'historique est sacré dans les trois cas.
    protected_ids: list[uuid.UUID] = []
    for status, soft_deleted in (("in_progress", False), ("closed", False), ("in_progress", True)):
        expat = await make_expat_user()
        profile = ClientProfile(
            agency_id=admin.agency_id,
            expat_user_id=expat.id,
            first_name="Avec",
            last_name="Dossier",
            email=expat.email,
        )
        db_session.add(profile)
        await db_session.commit()
        case = await make_client_case(
            agency_id=admin.agency_id,
            principal_expat_user_id=expat.id,
            status=status,
        )
        if soft_deleted:
            case.deleted_at = datetime.now(UTC)
            await db_session.commit()
        protected_ids.append(profile.id)

    report = await _bulk(client, headers, filter={})
    assert report["matching"] == 6
    assert report["protected"] == 3
    assert report["deletable"] == 3
    assert report["deleted"] == 3
    assert set(report["protected_ids"]) == {str(pid) for pid in protected_ids}

    survivors = set(
        (
            await db_session.execute(
                select(ClientProfile.id).where(ClientProfile.agency_id == admin.agency_id)
            )
        )
        .scalars()
        .all()
    )
    assert survivors == set(protected_ids)
    assert not survivors & set(free_ids)


async def test_bulk_and_unitary_agree_on_what_is_protected(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
) -> None:
    """La masse ne relâche RIEN : une fiche que le geste unitaire refuse
    de supprimer (409) est exactement une fiche que la masse protège."""
    headers = agent_headers(admin)
    expat = await make_expat_user()
    profile = ClientProfile(
        agency_id=admin.agency_id,
        expat_user_id=expat.id,
        first_name="Avec",
        last_name="Dossier",
        email=expat.email,
    )
    db_session.add(profile)
    await db_session.commit()
    await make_client_case(agency_id=admin.agency_id, principal_expat_user_id=expat.id)

    unitary = await client.delete(f"/client-profiles/{profile.id}", headers=headers)
    assert unitary.status_code == 409
    assert unitary.json()["code"] == "profile.has_cases"

    report = await _bulk(client, headers, ids=[str(profile.id)])
    assert report["protected"] == 1
    assert report["deleted"] == 0
    assert report["protected_ids"] == [str(profile.id)]


# ─── 3. Le compte annoncé est le compte réel ──────────────────────────────


async def test_dry_run_equals_execution(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
) -> None:
    """LE CONTRAT DU CHIFFRE ANNONCÉ : le dry-run rend les mêmes comptes
    que l'exécution, sur la même base, et n'écrit RIEN — ni fiche
    supprimée, ni ligne de journal (un compte n'est pas un geste)."""
    headers = agent_headers(admin)
    for _ in range(5):
        await _free_profile(db_session, admin.agency_id)
    expat = await make_expat_user()
    db_session.add(
        ClientProfile(
            agency_id=admin.agency_id,
            expat_user_id=expat.id,
            first_name="Avec",
            last_name="Dossier",
            email=expat.email,
        )
    )
    await db_session.commit()
    await make_client_case(agency_id=admin.agency_id, principal_expat_user_id=expat.id)

    before = (
        await db_session.execute(
            select(func.count())
            .select_from(ClientProfile)
            .where(ClientProfile.agency_id == admin.agency_id)
        )
    ).scalar_one()

    dry = await _bulk(client, headers, filter={}, dry_run=True)
    assert dry["dry_run"] is True
    assert dry["deleted"] == 0

    # Le dry-run n'a touché à rien.
    after_dry = (
        await db_session.execute(
            select(func.count())
            .select_from(ClientProfile)
            .where(ClientProfile.agency_id == admin.agency_id)
        )
    ).scalar_one()
    assert after_dry == before
    traces = (
        await db_session.execute(select(func.count()).select_from(BulkDeletionLog))
    ).scalar_one()
    assert traces == 0, "un dry-run a laissé une trace : ce n'est pas un geste"

    real = await _bulk(client, headers, filter={})
    assert real["dry_run"] is False
    # LE CŒUR : les trois comptes annoncés sont ceux du geste.
    assert (real["matching"], real["protected"], real["deletable"]) == (
        dry["matching"],
        dry["protected"],
        dry["deletable"],
    )
    assert real["deleted"] == dry["deletable"]


async def test_batches_do_not_lose_anyone(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
) -> None:
    """Plus d'un paquet : le découpage ne saute personne et ne compte
    personne deux fois (l'ordre de sélection est stable)."""
    from src.core.bulk_delete import BATCH_SIZE

    db_session.add_all(
        [
            ClientProfile(
                agency_id=admin.agency_id,
                first_name="Masse",
                last_name=f"N{index:04d}",
                email=f"masse{index}@example.com",
            )
            for index in range(BATCH_SIZE + 7)
        ]
    )
    await db_session.commit()

    report = await _bulk(client, agent_headers(admin), filter={})
    assert report["matching"] == BATCH_SIZE + 7
    assert report["deleted"] == BATCH_SIZE + 7
    left = (
        await db_session.execute(
            select(func.count())
            .select_from(ClientProfile)
            .where(ClientProfile.agency_id == admin.agency_id)
        )
    ).scalar_one()
    assert left == 0


# ─── 4. Le scopage agence, et la trace ────────────────────────────────────


async def test_never_reaches_beyond_the_agency(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    """Ni par filtre, ni par identifiants empruntés : la voisine ne perd
    jamais une fiche. Un id étranger n'est pas une erreur bruyante — il
    n'existe pas ici, donc il ne gonfle même pas `matching`."""
    other = await make_agent(role=system_roles["admin"])
    assert other.agency_id != admin.agency_id
    mine = await _free_profile(db_session, admin.agency_id)
    theirs = await _free_profile(db_session, other.agency_id)

    # Filtre large chez moi : la fiche de la voisine n'est pas visée.
    report = await _bulk(client, agent_headers(admin), filter={}, dry_run=True)
    assert report["matching"] == 1

    # Identifiant emprunté : ignoré, jamais supprimé.
    report = await _bulk(client, agent_headers(admin), ids=[str(theirs), str(mine)])
    assert report["matching"] == 1
    assert report["deleted"] == 1
    still_there = await db_session.get(ClientProfile, theirs)
    assert still_there is not None, "une agence a supprimé la fiche d'une autre"


async def test_execution_leaves_a_trace_that_survives_the_profiles(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
) -> None:
    """Les fiches disparaissent, le geste reste : qui, quand, quel
    critère, combien."""
    headers = agent_headers(admin)
    for _ in range(2):
        await _free_profile(db_session, admin.agency_id, tags=["purge"])

    await _bulk(client, headers, filter={"tags": ["purge"]})

    trace = (
        await db_session.execute(
            select(BulkDeletionLog).where(BulkDeletionLog.agency_id == admin.agency_id)
        )
    ).scalar_one()
    assert trace.entity == "client_profile"
    assert trace.performed_by_agent_id == admin.id  # QUI
    assert trace.performed_by_email == admin.email
    assert trace.created_at is not None  # QUAND
    assert trace.selector == {"mode": "filter", "filter": {"tags": ["purge"]}}  # QUEL CRITÈRE
    assert (trace.matching, trace.protected, trace.deletable, trace.deleted) == (2, 0, 2, 2)


async def test_bulk_delete_needs_the_delete_permission(
    client: AsyncClient,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    """La masse demande PLUS que l'unité : `case.delete`, pas `case.edit`.
    Un agent qui peut nettoyer une fiche ne vide pas l'annuaire par
    héritage."""
    member = await make_agent(role=system_roles["member"])
    response = await client.post(
        "/client-profiles/bulk-delete",
        headers=agent_headers(member),
        json={"filter": {}, "dry_run": True},
    )
    assert response.status_code == 403


async def test_missing_token_401(client: AsyncClient) -> None:
    response = await client.post("/client-profiles/bulk-delete", json={"filter": {}})
    assert response.status_code == 401


# ─── La face SOCIÉTÉ : le même contrat ────────────────────────────────────


async def test_company_bulk_delete_filter_protection_and_trace(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    make_client_case: MakeClientCase,
) -> None:
    """Le miroir société, sur les trois lois : le filtre vise ce que la
    liste montre, un dossier retient sa société, la trace reste."""
    headers = agent_headers(admin)
    for name in ("Alpha SA", "Beta SA", "Gamma SARL"):
        response = await client.post(
            "/company-profiles", headers=headers, json={"name": name, "tags": ["purge"]}
        )
        assert response.status_code == 201, response.text

    # Une société RETENUE par un dossier.
    held = (
        await db_session.execute(select(CompanyProfile).where(CompanyProfile.name == "Gamma SARL"))
    ).scalar_one()
    case = await make_client_case(agency_id=admin.agency_id)
    case.company_profile_id = held.id
    await db_session.commit()

    criteria = {"tags": ["purge"]}
    listed = await client.get("/company-profiles", headers=headers, params=criteria)
    assert listed.json()["total"] == 3

    dry = await client.post(
        "/company-profiles/bulk-delete",
        headers=headers,
        json={"filter": criteria, "dry_run": True},
    )
    assert dry.status_code == 200, dry.text
    assert dry.json()["matching"] == 3
    assert dry.json()["protected"] == 1
    assert dry.json()["deletable"] == 2

    real = await client.post(
        "/company-profiles/bulk-delete", headers=headers, json={"filter": criteria}
    )
    assert real.status_code == 200, real.text
    assert real.json()["deleted"] == 2
    assert real.json()["protected_ids"] == [str(held.id)]

    remaining = (
        (
            await db_session.execute(
                select(CompanyProfile.id).where(CompanyProfile.agency_id == admin.agency_id)
            )
        )
        .scalars()
        .all()
    )
    assert list(remaining) == [held.id]

    trace = (
        await db_session.execute(
            select(BulkDeletionLog).where(BulkDeletionLog.entity == "company_profile")
        )
    ).scalar_one()
    assert (trace.matching, trace.protected, trace.deleted) == (3, 1, 2)

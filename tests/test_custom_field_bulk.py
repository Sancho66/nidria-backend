"""LES GESTES DE MASSE sur les champs personnalisés — ranger un groupe
entier, d'un coup, sans surprise.

Le lot précédent a rendu la portée choisissable et rattrapable, mais un
clic à la fois. Une agence qui découvre que 24 de ses champs sont du
mauvais côté ne va pas cliquer 24 fois : elle a besoin du geste de masse.

Ce fichier tient les trois garanties qui rendent ce geste sûr :

1. LE COMPTE ANNONCÉ EST LE COMPTE APPLIQUÉ — une seule évaluation sert
   le dry-run et le geste. Un écran qui promet « 24 champs » ne peut pas
   en traiter 19. (Même principe éprouvé que l'aperçu d'import.)
2. RIEN NE PART EN SILENCE — un champ qu'un parcours collecte ou exige
   est refusé et NOMMÉ, avec ses parcours. Le franchir est un second
   geste explicite (`force`), jamais un effet de bord.
3. LES VALEURS NE BOUGENT JAMAIS — reclasser, ranger, archiver déplacent
   une surface, pas une donnée. Vérifié en base, pas au contrat.
"""

import uuid

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from shared.models.client_profile import ClientProfile
from shared.models.custom_field import CustomFieldDefinition
from shared.models.journey import JourneyTemplate, JourneyTemplateField
from shared.models.rbac import Role
from shared.models.step_requirement import StepRequirement
from shared.models.usage import UsageEvent
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent
from tests.plugins.journey_plugin import MakeJourneyTemplate, MakeTemplateStep

pytestmark = pytest.mark.usefixtures("rbac_baseline")

BULK = "/agencies/me/custom-fields/bulk"


@pytest_asyncio.fixture
async def admin(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    return await make_agent(role=system_roles["admin"])


async def _define(
    client: AsyncClient, headers: dict[str, str], key: str, **overrides: object
) -> dict:
    payload = {
        "key": key,
        "label": key.replace("_", " ").capitalize(),
        "field_type": "text",
        **overrides,
    }
    r = await client.post("/agencies/me/custom-fields", headers=headers, json=payload)
    assert r.status_code == 201, r.text
    return r.json()


async def _collected_by(
    db: AsyncSession, template: JourneyTemplate, key: str
) -> JourneyTemplateField:
    """Attache un champ à la COLLECTE d'un parcours (le formulaire de
    création de dossier) — c'est la référence par CLÉ que la protection
    doit voir."""
    field = JourneyTemplateField(template_id=template.id, kind="custom_field", reference=key)
    db.add(field)
    await db.commit()
    return field


async def _stored(db: AsyncSession, field_id: str) -> CustomFieldDefinition:
    definition = await db.get(CustomFieldDefinition, uuid.UUID(field_id))
    assert definition is not None
    await db.refresh(definition)
    return definition


# --- reclasser en masse ---------------------------------------------------------------


async def test_the_whole_selection_changes_scope_in_one_gesture(
    client: AsyncClient, db_session: AsyncSession, admin: Agent, agent_headers: AuthHeaders
) -> None:
    """LE GESTE du lot : trois champs « mission » deviennent des champs de
    fiche en un appel. C'est l'écran de Nicolas, en une fois."""
    headers = agent_headers(admin)
    ids = [
        (await _define(client, headers, k, scope="case"))["id"]
        for k in ("a_one", "a_two", "a_three")
    ]

    r = await client.post(
        BULK, headers=headers, json={"ids": ids, "action": "scope", "scope": "person"}
    )
    assert r.status_code == 200, r.text
    report = r.json()
    assert (report["requested"], report["eligible"], report["applied"]) == (3, 3, 3)
    assert report["refused"] == 0 and report["unchanged"] == 0
    for field_id in ids:
        assert (await _stored(db_session, field_id)).scope == "person"


async def test_a_field_already_on_the_target_side_is_unchanged_not_refused(
    client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    """Une sélection large attrape des champs déjà bien classés : ils ne
    sont ni traités ni refusés, ils sont COMPTÉS à part. Sans ça, l'écran
    annoncerait des refus là où il n'y a qu'un no-op."""
    headers = agent_headers(admin)
    already = (await _define(client, headers, "b_one", scope="person"))["id"]
    to_move = (await _define(client, headers, "b_two", scope="case"))["id"]

    r = await client.post(
        BULK,
        headers=headers,
        json={"ids": [already, to_move], "action": "scope", "scope": "person"},
    )
    report = r.json()
    assert (report["eligible"], report["applied"], report["unchanged"]) == (1, 1, 1)
    assert report["refused"] == 0


async def test_the_whole_selection_is_ranged_in_one_section(
    client: AsyncClient, db_session: AsyncSession, admin: Agent, agent_headers: AuthHeaders
) -> None:
    """« Ranger tout Divers dans Fiscalité » — le cas d'usage exact."""
    headers = agent_headers(admin)
    ids = [(await _define(client, headers, k, scope="person"))["id"] for k in ("c_one", "c_two")]

    r = await client.post(
        BULK,
        headers=headers,
        json={"ids": ids, "action": "section", "profile_section": "situation"},
    )
    assert r.json()["applied"] == 2
    for field_id in ids:
        assert (await _stored(db_session, field_id)).profile_section == "situation"


# --- la protection parcours -----------------------------------------------------------


async def test_a_field_a_journey_collects_is_refused_and_named(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    make_journey_template: MakeJourneyTemplate,
) -> None:
    """LA PROTECTION : archiver ne peut pas retirer en silence un champ
    qu'un parcours collecte. Le refus NOMME le champ et ses parcours —
    l'écran doit pouvoir écrire « Type de permis (Visa retraité) », pas
    « 1 champ refusé »."""
    headers = agent_headers(admin)
    template = await make_journey_template(agency_id=admin.agency_id, name="Visa retraite")
    used = (await _define(client, headers, "d_used", scope="person"))["id"]
    free = (await _define(client, headers, "d_free", scope="person"))["id"]
    await _collected_by(db_session, template, "d_used")

    r = await client.post(BULK, headers=headers, json={"ids": [used, free], "action": "archive"})
    report = r.json()
    assert (report["eligible"], report["applied"], report["refused"]) == (1, 1, 1)
    refusal = report["refusals"][0]
    assert refusal["id"] == used
    assert refusal["reason"] == "used_in_journey"
    assert refusal["key"] == "d_used"
    assert refusal["templates"] == ["Visa retraite"]
    # Le refusé est INTACT, le libre est parti : un refus n'annule pas le reste.
    assert (await _stored(db_session, used)).archived_at is None
    assert (await _stored(db_session, free)).archived_at is not None


async def test_a_field_a_step_requires_is_protected_too(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    make_journey_template: MakeJourneyTemplate,
    make_template_step: MakeTemplateStep,
) -> None:
    """Un parcours référence un champ à DEUX endroits : la collecte à la
    création et les exigences d'étape. Le dégât est le même — la
    protection couvre les deux."""
    headers = agent_headers(admin)
    template = await make_journey_template(agency_id=admin.agency_id, name="Permis de travail")
    step = await make_template_step(template=template)
    field_id = (await _define(client, headers, "e_required", scope="person"))["id"]
    db_session.add(
        StepRequirement(
            step_id=step.id, kind="custom_field", reference="e_required", scope="principal"
        )
    )
    await db_session.commit()

    r = await client.post(BULK, headers=headers, json={"ids": [field_id], "action": "archive"})
    report = r.json()
    assert report["refused"] == 1
    assert report["refusals"][0]["templates"] == ["Permis de travail"]


async def test_force_archives_the_used_field_and_says_so_in_the_trace(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    make_journey_template: MakeJourneyTemplate,
) -> None:
    """La protection n'est pas un mur : le reste du système tolère un
    champ archivé attaché (il reste listé, drapeau `is_archived`). Le
    franchissement est un SECOND geste explicite, et il laisse une trace
    qui dit qu'il a été forcé."""
    headers = agent_headers(admin)
    template = await make_journey_template(agency_id=admin.agency_id, name="Regroupement")
    field_id = (await _define(client, headers, "f_used", scope="person"))["id"]
    await _collected_by(db_session, template, "f_used")

    r = await client.post(
        BULK, headers=headers, json={"ids": [field_id], "action": "archive", "force": True}
    )
    report = r.json()
    assert (report["applied"], report["refused"], report["in_journey"]) == (1, 0, 1)
    assert (await _stored(db_session, field_id)).archived_at is not None

    events = (
        await db_session.execute(
            select(UsageEvent).where(
                UsageEvent.agency_id == admin.agency_id,
                UsageEvent.event_type == "agency.custom_fields_set",
            )
        )
    ).scalars()
    forced = [e for e in events if e.details.get("bulk") == "archive"]
    assert len(forced) == 1
    assert forced[0].details["forced"] is True
    assert forced[0].details["keys"] == ["f_used"]
    assert forced[0].actor_id == admin.id


async def test_the_unit_archive_refuses_the_same_way_and_force_passes(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    make_journey_template: MakeJourneyTemplate,
) -> None:
    """La protection vaut À L'UNITÉ comme en masse — sinon archiver un par
    un serait le contournement de la protection de masse."""
    headers = agent_headers(admin)
    template = await make_journey_template(agency_id=admin.agency_id, name="Etudiant")
    field_id = (await _define(client, headers, "g_used", scope="person"))["id"]
    await _collected_by(db_session, template, "g_used")

    refused = await client.post(f"/agencies/me/custom-fields/{field_id}/archive", headers=headers)
    assert refused.status_code == 409, refused.text
    body = refused.json()
    assert body["code"] == "custom_field.used_in_journey"
    assert body["params"]["templates"] == ["Etudiant"]
    assert (await _stored(db_session, field_id)).archived_at is None

    forced = await client.post(
        f"/agencies/me/custom-fields/{field_id}/archive?force=true", headers=headers
    )
    assert forced.status_code == 200, forced.text
    assert (await _stored(db_session, field_id)).archived_at is not None


# --- le compte avant le geste ---------------------------------------------------------


async def test_the_dry_run_announces_exactly_what_the_run_does(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    make_journey_template: MakeJourneyTemplate,
) -> None:
    """LE TÉMOIN CENTRAL : l'aperçu et le geste sortent de la MÊME
    évaluation. On compare les deux rapports champ par champ (hors
    `applied`/`dry_run`), et on vérifie qu'entre les deux la base n'a pas
    bougé d'un octet."""
    headers = agent_headers(admin)
    template = await make_journey_template(agency_id=admin.agency_id, name="Investisseur")
    used = (await _define(client, headers, "h_used", scope="person"))["id"]
    free = (await _define(client, headers, "h_free", scope="person"))["id"]
    gone = (await _define(client, headers, "h_gone", scope="person"))["id"]
    await _collected_by(db_session, template, "h_used")
    await client.post(f"/agencies/me/custom-fields/{gone}/archive", headers=headers)
    body = {"ids": [used, free, gone, str(uuid.uuid4())], "action": "archive"}

    preview = (await client.post(BULK, headers=headers, json={**body, "dry_run": True})).json()
    assert preview["dry_run"] is True and preview["applied"] == 0
    # RIEN n'a été écrit par l'aperçu.
    assert (await _stored(db_session, free)).archived_at is None

    real = (await client.post(BULK, headers=headers, json=body)).json()
    assert real["applied"] == preview["eligible"] == 1
    for field in ("requested", "eligible", "unchanged", "refused", "in_journey", "refusals"):
        assert preview[field] == real[field], field
    assert preview["unchanged"] == 1  # `gone`, déjà archivé
    assert preview["refused"] == 2  # `used` + l'id inconnu


async def test_a_foreign_id_is_refused_without_leaking_anything(
    client: AsyncClient,
    admin: Agent,
    agent_headers: AuthHeaders,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
) -> None:
    """Le scope agence tient dans le geste de masse : l'id d'une autre
    agence est `not_found` — même verdict qu'un id inexistant, et le
    rapport n'en dit ni la clé ni le libellé."""
    other_admin = await make_agent(role=system_roles["admin"])
    foreign = (await _define(client, agent_headers(other_admin), "i_secret", scope="person"))["id"]

    headers = agent_headers(admin)
    mine = (await _define(client, headers, "i_mine", scope="case"))["id"]
    r = await client.post(
        BULK, headers=headers, json={"ids": [mine, foreign], "action": "scope", "scope": "person"}
    )
    report = r.json()
    assert (report["eligible"], report["refused"]) == (1, 1)
    refusal = report["refusals"][0]
    assert refusal["reason"] == "not_found"
    assert refusal["key"] is None and refusal["label"] is None


async def test_the_values_are_counted_and_never_touched(
    client: AsyncClient, db_session: AsyncSession, admin: Agent, agent_headers: AuthHeaders
) -> None:
    """Ce que l'écran promet — « les valeurs déjà saisies ne bougent
    pas » — est vérifié ici en base, sur les deux comptes servis : combien
    de champs portent des valeurs, et combien de valeurs sont concernées."""
    headers = agent_headers(admin)
    filled = (await _define(client, headers, "j_filled", scope="person"))["id"]
    empty = (await _define(client, headers, "j_empty", scope="person"))["id"]
    profiles = []
    for i in range(2):
        created = await client.post(
            "/client-profiles",
            headers=headers,
            json={"first_name": f"P{i}", "last_name": "Test", "email": f"p{i}@example.com"},
        )
        profile_id = created.json()["id"]
        await client.patch(
            f"/client-profiles/{profile_id}",
            headers=headers,
            json={"custom_fields": {"j_filled": f"valeur {i}"}},
        )
        profiles.append(profile_id)

    r = await client.post(
        BULK,
        headers=headers,
        json={"ids": [filled, empty], "action": "section", "profile_section": "contact"},
    )
    report = r.json()
    assert report["with_values"] == 1  # un seul des deux champs porte des valeurs
    assert report["values_count"] == 2  # sur deux fiches
    for profile_id, expected in zip(profiles, ("valeur 0", "valeur 1"), strict=True):
        profile = await db_session.get(ClientProfile, uuid.UUID(profile_id))
        assert profile is not None
        await db_session.refresh(profile)
        assert profile.custom_fields["j_filled"] == expected


# --- le gate --------------------------------------------------------------------------


async def test_the_bulk_gesture_is_gated_like_the_unit_one(
    client: AsyncClient,
    admin: Agent,
    agent_headers: AuthHeaders,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
) -> None:
    """Traiter 24 champs d'un coup n'est pas un droit de plus : c'est le
    même `field.manage`. Un membre ne requalifie pas l'univers de son
    agence, même en masse."""
    field_id = (await _define(client, agent_headers(admin), "k_one", scope="case"))["id"]
    member = await make_agent(agency_id=admin.agency_id, role=system_roles["member"])
    denied = await client.post(
        BULK,
        headers=agent_headers(member),
        json={"ids": [field_id], "action": "scope", "scope": "person"},
    )
    assert denied.status_code == 403, denied.text


async def test_an_action_without_its_target_is_refused_at_the_contract(
    client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    """« Reclasser » sans dire vers quoi n'est pas un geste : 422 au
    contrat, jamais un geste vide qui rapporte 0."""
    headers = agent_headers(admin)
    field_id = (await _define(client, headers, "l_one", scope="case"))["id"]
    for body in (
        {"ids": [field_id], "action": "scope"},
        {"ids": [field_id], "action": "section"},
        {"ids": [field_id], "action": "archive", "scope": "person"},
        {"ids": [], "action": "archive"},
    ):
        r = await client.post(BULK, headers=headers, json=body)
        assert r.status_code == 422, (body, r.text)

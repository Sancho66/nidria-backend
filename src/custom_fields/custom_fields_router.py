import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from shared.models.company_profile import CompanyFieldDefinition
from shared.models.custom_field import CustomFieldDefinition
from src.company_profiles.company_catalog import (
    company_definition_response,
    materialize_company_definitions,
)
from src.core.dependencies import get_current_agent, get_db
from src.core.enums import Audience
from src.core.i18n import RequestLang, resolve_i18n
from src.core.rbac.baseline import RouteBinding
from src.core.rbac.permissions import Permission
from src.custom_fields.custom_fields_manager import CustomFieldsManager
from src.custom_fields.custom_fields_schema import (
    CustomFieldBulkReport,
    CustomFieldBulkRequest,
    CustomFieldDefinitionCreate,
    CustomFieldDefinitionResponse,
    CustomFieldDefinitionUpdate,
    CustomFieldOrderRequest,
    FieldUniverseResponse,
)
from src.custom_fields.field_universe import Surface, field_universe

router = APIRouter(prefix="/agencies/me/custom-fields", tags=["custom-fields"])
# L'univers affiché ne vit pas sous le préfixe des définitions : il ne
# parle pas de définitions, il parle d'écrans.
universe_router = APIRouter(tags=["custom-fields"])

# Read = case.view (every agent rendering the person form needs the
# definitions); mutations = field.manage (admin config). Same
# read/manage split as roles.
BINDINGS = [
    RouteBinding("GET", "/agencies/me/custom-fields", Audience.AGENT, Permission.CASE_VIEW),
    RouteBinding("POST", "/agencies/me/custom-fields", Audience.AGENT, Permission.FIELD_MANAGE),
    RouteBinding(
        "PATCH", "/agencies/me/custom-fields/{field_id}", Audience.AGENT, Permission.FIELD_MANAGE
    ),
    RouteBinding(
        "POST",
        "/agencies/me/custom-fields/{field_id}/archive",
        Audience.AGENT,
        Permission.FIELD_MANAGE,
    ),
    RouteBinding(
        "POST",
        "/agencies/me/custom-fields/{field_id}/unarchive",
        Audience.AGENT,
        Permission.FIELD_MANAGE,
    ),
    # Les gestes de masse : même gate que les gestes à l'unité — traiter
    # 24 champs d'un coup n'est pas un droit de plus, c'est le même droit.
    RouteBinding(
        "POST", "/agencies/me/custom-fields/bulk", Audience.AGENT, Permission.FIELD_MANAGE
    ),
    # D12 — réécrire l'ordre entier : même gate que le PATCH de position
    # qu'il remplace à l'échelle de la liste.
    RouteBinding(
        "PUT", "/agencies/me/custom-fields/order", Audience.AGENT, Permission.FIELD_MANAGE
    ),
    # L'univers affiché est une LECTURE, comme la liste des définitions :
    # même `case.view` (tout agent qui rend un formulaire en a besoin).
    RouteBinding("GET", "/agencies/me/field-universe", Audience.AGENT, Permission.CASE_VIEW),
]

DbDep = Annotated[AsyncSession, Depends(get_db)]
AgentDep = Annotated[Agent, Depends(get_current_agent)]


def _definition_response(
    definition: CustomFieldDefinition | CompanyFieldDefinition,
) -> CustomFieldDefinitionResponse:
    """Les trois gestes (PATCH, archive, unarchive) adressent une
    définition par son id, sans savoir de quelle face elle vient — c'est
    ce qui permet aux capacités société d'exister sans que l'écran
    apprenne une seconde route. Le mapper rend les deux dans LE contrat
    des définitions."""
    if isinstance(definition, CompanyFieldDefinition):
        return company_definition_response(definition)
    return CustomFieldDefinitionResponse.model_validate(definition)


@router.get("", response_model=list[CustomFieldDefinitionResponse])
async def list_custom_fields(
    agent: AgentDep,
    db: DbDep,
    lang: RequestLang,
    include_archived: Annotated[bool, Query()] = False,
    surface: Annotated[Surface | None, Query()] = None,
) -> list[CustomFieldDefinitionResponse]:
    """`surface` CHOISIT LA FACE, et c'est ce qui répare un mensonge
    ancien : la fiche société résolvait ses références contre cette
    liste, qui ne contient que des définitions personne/dossier — d'où
    les 9 clés (`company_name`, `legal_form`…) déclarées côté PERSONNE
    qui pilotaient l'affichage de la fiche SOCIÉTÉ.

    `surface=company` sert les définitions société (`scope` rendu à
    `"company"`), `person`/`case` filtrent la portée, et l'ABSENCE garde
    le comportement d'avant (personne + dossier) — un appelant existant
    qui se tait garde ce qu'il avait."""
    mgr = CustomFieldsManager(db)
    if surface == "company":
        # MATÉRIALISE, comme l'univers et la fiche : demander les
        # définitions société d'une agence qui n'a encore rien ouvert doit
        # rendre son univers, pas une liste vide. Sans ça, le tout premier
        # chargement d'écran dépendrait de l'ORDRE des deux appels du
        # front — une course invisible qui se résout « au rechargement ».
        rows = await materialize_company_definitions(db, agent.agency_id)
        await db.commit()
        agency_default = await mgr.agency_default(agent.agency_id)
        return [
            company_definition_response(row).model_copy(
                update={"label": resolve_i18n(row.label_i18n, lang, agency_default, row.label)}
            )
            for row in sorted(rows, key=lambda r: (r.position, r.key))
            if include_archived or row.archived_at is None
        ]
    definitions = await mgr.list_definitions(agent, include_archived=include_archived)
    if surface is not None:
        definitions = [d for d in definitions if d.scope == surface]
    agency_default = await mgr.agency_default(agent.agency_id)
    # i18n: resolve the LABEL for the display language (the `key` stays raw).
    return [
        CustomFieldDefinitionResponse.model_validate(d).model_copy(
            update={"label": resolve_i18n(d.label_i18n, lang, agency_default, d.label)}
        )
        for d in definitions
    ]


@router.post("", response_model=CustomFieldDefinitionResponse, status_code=201)
async def create_custom_field(
    body: CustomFieldDefinitionCreate, agent: AgentDep, db: DbDep
) -> CustomFieldDefinitionResponse:
    """`scope='company'` crée un champ de FICHE SOCIÉTÉ (D9) — même route,
    même contrat de sortie que les deux autres portées, parce que c'est le
    même geste. Ce que cette face change, et rien d'autre : la `key` est
    DÉRIVÉE du libellé (l'envoyer est refusé), la `profile_section` est
    requise, et `options`/`required` n'y existent pas.

    Collision avec un preset du catalogue → 409
    `company_field.key_reserved` ; avec une clé déjà prise par l'agence
    (archivée comprise) → 409 `company_field.key_exists`, qui NOMME la
    définition en place pour que l'écran propose de la renommer ou de la
    ressusciter."""
    definition = await CustomFieldsManager(db).create(agent, body)
    return _definition_response(definition)


@router.patch("/{field_id}", response_model=CustomFieldDefinitionResponse)
async def update_custom_field(
    field_id: uuid.UUID, body: CustomFieldDefinitionUpdate, agent: AgentDep, db: DbDep
) -> CustomFieldDefinitionResponse:
    """key and field_type are immutable — archive + recreate to change
    a type."""
    definition = await CustomFieldsManager(db).update(agent, field_id, body)
    return _definition_response(definition)


@universe_router.get("/agencies/me/field-universe", response_model=FieldUniverseResponse)
async def get_field_universe(
    agent: AgentDep,
    db: DbDep,
    lang: RequestLang,
    surface: Annotated[Surface, Query()] = "person",
) -> FieldUniverseResponse:
    """L'univers AFFICHÉ d'un écran, pas le stockage : les sections dans
    l'ordre de l'écran, et chaque champ avec son état (ce qu'on peut en
    faire) plutôt qu'une déduction laissée au front."""
    return await field_universe(db, agent, surface, lang)


@router.put("/order", response_model=list[CustomFieldDefinitionResponse])
async def reorder_custom_fields(
    body: CustomFieldOrderRequest, agent: AgentDep, db: DbDep, lang: RequestLang
) -> list[CustomFieldDefinitionResponse]:
    """D12 — la liste ordonnée des definition_id, réécrite en une
    transaction (renumérotation 1..N, unicité garantie). L'ensemble doit
    être EXACT : absent, étranger ou doublon → 422 nommé, ordre intact.
    Rend la liste dans l'ordre gravé — ce que l'écran affiche ensuite EST
    ce que la base a écrit."""
    manager = CustomFieldsManager(db)
    await manager.reorder(agent, body.field_ids)
    definitions = await manager.list_definitions(agent)
    agency_default = await manager.agency_default(agent.agency_id)
    return [
        CustomFieldDefinitionResponse.model_validate(d).model_copy(
            update={"label": resolve_i18n(d.label_i18n, lang, agency_default, d.label)}
        )
        for d in definitions
    ]


@router.post("/bulk", response_model=CustomFieldBulkReport)
async def bulk_custom_fields(
    body: CustomFieldBulkRequest, agent: AgentDep, db: DbDep
) -> CustomFieldBulkReport:
    """Archiver / reclasser / ranger une SÉLECTION, en une transaction.
    `dry_run=true` rend le même rapport sans écrire : c'est ce qui permet
    d'annoncer les conséquences avant le geste."""
    return await CustomFieldsManager(db).bulk(agent, body)


@router.post("/{field_id}/archive", response_model=CustomFieldDefinitionResponse)
async def archive_custom_field(
    field_id: uuid.UUID,
    agent: AgentDep,
    db: DbDep,
    force: Annotated[bool, Query()] = False,
) -> CustomFieldDefinitionResponse:
    """Soft archive (the only removal). Saved values are kept. Refusé
    (409) si un parcours collecte ou exige le champ — `force=true` le
    franchit explicitement."""
    definition = await CustomFieldsManager(db).archive(agent, field_id, force=force)
    return _definition_response(definition)


@router.post("/{field_id}/unarchive", response_model=CustomFieldDefinitionResponse)
async def unarchive_custom_field(
    field_id: uuid.UUID, agent: AgentDep, db: DbDep
) -> CustomFieldDefinitionResponse:
    """Resurrect an archived field — it reappears in forms and its kept
    JSONB values become exposed/validable again. Idempotent."""
    definition = await CustomFieldsManager(db).unarchive(agent, field_id)
    return _definition_response(definition)

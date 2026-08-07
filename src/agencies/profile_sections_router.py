"""Les SECTIONS DE FICHE d'une agence — lire, créer, renommer, ranger,
supprimer.

Même partage de droits que les champs, et pour la même raison : lire est
`case.view` (tout agent qui rend une fiche a besoin de la liste des
sections), configurer est `field.manage` (c'est de la configuration
d'agence, pas du travail de dossier).
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from src.agencies.profile_sections_manager import (
    create_section,
    delete_section,
    list_sections,
    rename_section,
    section_name,
)
from src.agencies.profile_sections_schema import (
    ProfileSectionCreateRequest,
    ProfileSectionResponse,
    ProfileSectionUpdateRequest,
)
from src.core.dependencies import get_current_agent, get_db
from src.core.enums import Audience
from src.core.i18n import RequestLang
from src.core.rbac.baseline import RouteBinding
from src.core.rbac.permissions import Permission
from src.custom_fields.field_universe import Surface

router = APIRouter(prefix="/agencies/me/profile-sections", tags=["profile-sections"])

BINDINGS = [
    RouteBinding("GET", "/agencies/me/profile-sections", Audience.AGENT, Permission.CASE_VIEW),
    RouteBinding("POST", "/agencies/me/profile-sections", Audience.AGENT, Permission.FIELD_MANAGE),
    RouteBinding(
        "PATCH",
        "/agencies/me/profile-sections/{section_id}",
        Audience.AGENT,
        Permission.FIELD_MANAGE,
    ),
    RouteBinding(
        "DELETE",
        "/agencies/me/profile-sections/{section_id}",
        Audience.AGENT,
        Permission.FIELD_MANAGE,
    ),
]

DbDep = Annotated[AsyncSession, Depends(get_db)]
AgentDep = Annotated[Agent, Depends(get_current_agent)]


def _response(section: object, lang: str, agency_default: str) -> ProfileSectionResponse:
    from shared.models.agency import AgencyProfileSection
    from src.agencies.profile_sections_manager import MISC, catalog_label_i18n

    assert isinstance(section, AgencyProfileSection)
    return ProfileSectionResponse(
        id=section.id,
        key=section.key,
        surface=section.surface,
        name=section_name(section, lang, agency_default),
        # Le blob SERVI est celui du repli quand l'agence n'a pas renommé :
        # l'écran voit les 7 langues du produit, pas un dictionnaire vide
        # qu'il devrait interpréter.
        name_i18n=section.label_i18n or catalog_label_i18n(section.key),
        # `customized` dit d'où vient le libellé — c'est ce qui permet à
        # l'écran de proposer « rétablir le nom d'origine » sans deviner.
        customized=bool(section.label_i18n),
        position=section.position,
        deletable=section.key != MISC,
    )


async def _agency_default(db: AsyncSession, agency_id: uuid.UUID) -> str:
    from src.custom_fields.custom_fields_manager import CustomFieldsManager

    return await CustomFieldsManager(db).agency_default(agency_id)


@router.get("", response_model=list[ProfileSectionResponse])
async def list_profile_sections(
    agent: AgentDep,
    db: DbDep,
    lang: RequestLang,
    surface: Annotated[Surface, Query()] = "person",
) -> list[ProfileSectionResponse]:
    """Les sections d'une surface, dans l'ordre de l'écran. Matérialise
    les 4 d'origine si l'agence n'a jamais ouvert cette surface."""
    sections = await list_sections(db, agent.agency_id, surface)
    await db.commit()
    agency_default = await _agency_default(db, agent.agency_id)
    return [_response(s, lang, agency_default) for s in sections]


@router.post("", response_model=ProfileSectionResponse, status_code=201)
async def create_profile_section(
    body: ProfileSectionCreateRequest, agent: AgentDep, db: DbDep, lang: RequestLang
) -> ProfileSectionResponse:
    """Créer une section. La CLÉ est posée ici et ne changera plus : ce
    sont les définitions de champs qui la portent."""
    section = await create_section(
        db,
        agent.agency_id,
        surface=body.surface,
        key=body.key,
        label_i18n=body.label_i18n,
        position=body.position,
    )
    await db.commit()
    await db.refresh(section)
    agency_default = await _agency_default(db, agent.agency_id)
    return _response(section, lang, agency_default)


@router.patch("/{section_id}", response_model=ProfileSectionResponse)
async def update_profile_section(
    section_id: uuid.UUID,
    body: ProfileSectionUpdateRequest,
    agent: AgentDep,
    db: DbDep,
    lang: RequestLang,
) -> ProfileSectionResponse:
    """Renommer et/ou déplacer. `label_i18n = {}` REND le libellé
    d'origine (le repli catalogue reprend la main) sans toucher aux champs
    que la section porte. « Divers » ne se renomme jamais."""
    provided = body.model_dump(exclude_unset=True)
    section = await rename_section(
        db,
        agent.agency_id,
        section_id,
        label_i18n=body.label_i18n if "label_i18n" in provided else None,
        position=body.position,
    )
    await db.commit()
    await db.refresh(section)
    agency_default = await _agency_default(db, agent.agency_id)
    return _response(section, lang, agency_default)


@router.delete("/{section_id}", status_code=204)
async def delete_profile_section(section_id: uuid.UUID, agent: AgentDep, db: DbDep) -> None:
    """Supprimer une section VIDE. Refus nommé (422) si elle porte des
    champs ou des colonnes civiles — « déplacez d'abord ses champs ».
    « Divers » ne se supprime jamais."""
    await delete_section(db, agent.agency_id, section_id)
    await db.commit()

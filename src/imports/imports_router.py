"""Endpoints for the CRM import feature.

- BLOC 1: read-only CRM referential (served from memory, no DB).
- BLOC 2: the transactional case import (POST /imports/cases).
- BLOC 3: saved-mapping CRUD (/imports/mappings) — agency-scoped.

All routes are gated by `import.manage` (Audience.AGENT): admin and
case_manager hold it by default, viewer/member do not.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from src.core.dependencies import get_current_agent, get_db
from src.core.email import send_email
from src.core.enums import Audience
from src.core.rbac.baseline import RouteBinding
from src.core.rbac.permissions import Permission
from src.imports.case_import_manager import CaseImportManager
from src.imports.case_import_schema import CaseImportRequest, ImportPreview, ImportReport
from src.imports.imports_manager import ImportsManager
from src.imports.imports_schema import CrmDetailResponse, CrmListResponse
from src.imports.mapping_manager import MappingManager
from src.imports.mapping_schema import MappingListResponse, MappingResponse, MappingUpsertRequest
from src.imports.profile_import_manager import (
    CompanyImportManager,
    CompanyImportPreviewRequest,
    CompanyImportPreviewResponse,
    CompanyImportReport,
    CompanyImportRequest,
    ProfileImportManager,
    ProfileImportPreviewRequest,
    ProfileImportPreviewResponse,
    ProfileImportReport,
    ProfileImportRequest,
)

router = APIRouter(prefix="/imports", tags=["imports"])

BINDINGS = [
    RouteBinding("GET", "/imports/crms", Audience.AGENT, Permission.IMPORT_MANAGE),
    RouteBinding("GET", "/imports/crms/{slug}", Audience.AGENT, Permission.IMPORT_MANAGE),
    RouteBinding("POST", "/imports/cases", Audience.AGENT, Permission.IMPORT_MANAGE),
    RouteBinding("POST", "/imports/client-profiles", Audience.AGENT, Permission.IMPORT_MANAGE),
    RouteBinding(
        "POST", "/imports/client-profiles/preview", Audience.AGENT, Permission.IMPORT_MANAGE
    ),
    RouteBinding(
        "POST",
        "/imports/client-profiles/suggest-mapping",
        Audience.AGENT,
        Permission.IMPORT_MANAGE,
    ),
    RouteBinding(
        "POST",
        "/imports/company-profiles/suggest-mapping",
        Audience.AGENT,
        Permission.IMPORT_MANAGE,
    ),
    RouteBinding("POST", "/imports/company-profiles", Audience.AGENT, Permission.IMPORT_MANAGE),
    RouteBinding(
        "POST", "/imports/company-profiles/preview", Audience.AGENT, Permission.IMPORT_MANAGE
    ),
    RouteBinding("POST", "/imports/cases/preview", Audience.AGENT, Permission.IMPORT_MANAGE),
    RouteBinding("GET", "/imports/mappings", Audience.AGENT, Permission.IMPORT_MANAGE),
    RouteBinding("GET", "/imports/mappings/resolve", Audience.AGENT, Permission.IMPORT_MANAGE),
    RouteBinding("POST", "/imports/mappings", Audience.AGENT, Permission.IMPORT_MANAGE),
    RouteBinding(
        "DELETE", "/imports/mappings/{mapping_id}", Audience.AGENT, Permission.IMPORT_MANAGE
    ),
]

DbDep = Annotated[AsyncSession, Depends(get_db)]
AgentDep = Annotated[Agent, Depends(get_current_agent)]


@router.get("/crms", response_model=CrmListResponse)
async def list_crms() -> CrmListResponse:
    return ImportsManager().list_crms()


@router.get("/crms/{slug}", response_model=CrmDetailResponse)
async def get_crm(slug: str) -> CrmDetailResponse:
    return ImportsManager().get_crm(slug)


@router.post("/cases", response_model=ImportReport)
async def import_cases(
    body: CaseImportRequest,
    agent: AgentDep,
    db: DbDep,
    background: BackgroundTasks,
) -> ImportReport:
    """Create N dossiers from a CSV + mapping. Returns the report
    immediately; invitation emails are dispatched OUT of the request via
    BackgroundTasks (never N synchronous sends in the request path)."""
    report, pending = await CaseImportManager(db).run_import(agent, body)
    for mail in pending:
        background.add_task(send_email, mail.to, mail.subject, mail.text, mail.html)
    return report


@router.post("/client-profiles", response_model=ProfileImportReport)
async def import_client_profiles(
    body: ProfileImportRequest, agent: AgentDep, db: DbDep
) -> ProfileImportReport:
    """V4a — l'import repointé FICHES : colonnes→champs person, dédup
    email (lier, pas dupliquer), SANS parcours (étape séparée optionnelle
    — le wizard dossiers existant la garde). Aucun mail : une fiche
    n'invite personne."""
    return await ProfileImportManager(db).run_import(agent, body)


class SuggestMappingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    headers: list[str] = Field(min_length=1, max_length=200)


class SuggestMappingResponse(BaseModel):
    suggestions: dict[str, str]
    # L'ambiguïté SE PROPOSE, elle ne se devine pas : plusieurs cibles
    # offertes au combobox, rien d'auto-posé (« Pays » → nationalité OU
    # pays de résidence fiscale).
    ambiguous: dict[str, list[str]] = {}
    unmatched: list[str]


@router.post("/client-profiles/suggest-mapping", response_model=SuggestMappingResponse)
async def suggest_client_profiles_mapping(
    body: SuggestMappingRequest, agent: AgentDep, db: DbDep
) -> SuggestMappingResponse:
    """Lot mapping — l'auto-suggestion BACK : la table d'alias exhaustive
    (exclusions d'abord — les faux amis ne sont jamais suggérés), alias
    exacts FR/EN, replis (Mobile→phone), défs d'agence en dynamique,
    fuzzy prudent en dernier recours."""
    from src.client_profiles.backfill import CIVIL_COLUMNS
    from src.client_profiles.client_profiles_manager import person_scope_definitions
    from src.client_profiles.profile_sections import PRESET_PROFILE_SECTION
    from src.imports.header_aliases import (
        PERSON_ALIASES,
        PERSON_AMBIGUOUS,
        PERSON_EXCLUDED,
        PERSON_FALLBACK_ALIASES,
        normalize_header,
        suggest_mapping,
    )
    from src.imports.profile_import_manager import IDENTITY_TARGETS
    from src.journeys.field_catalog import FIELD_PRESETS

    person_defs = await person_scope_definitions(db, agent.agency_id)
    # LOT PLAFOND : le catalogue ENTIER est cible (un preset non déclaré
    # se déclare à l'import) — le vocabulaire dynamique porte les clés et
    # TOUS les labels i18n des presets person + les défs de l'agence.
    from src.imports.value_normalizers import ADDRESS_SUBFIELDS

    address_bases = {d.key for d in person_defs if d.field_type == "address"} | {
        k
        for k in PRESET_PROFILE_SECTION
        if FIELD_PRESETS.get(k) is not None and FIELD_PRESETS[k].field_type == "address"
    }
    valid = (
        set(IDENTITY_TARGETS)
        | set(CIVIL_COLUMNS)
        | {d.key for d in person_defs}
        | set(PRESET_PROFILE_SECTION)
        | {f"{b}.{sub}" for b in address_bases for sub in ADDRESS_SUBFIELDS}
        | {"tags", "preferred_lang"}
    )
    dynamic = {normalize_header(d.key): d.key for d in person_defs}
    dynamic.update({normalize_header(d.label): d.key for d in person_defs if d.label})
    for key in PRESET_PROFILE_SECTION:
        preset = FIELD_PRESETS.get(key)
        if preset is None:
            continue
        dynamic.setdefault(normalize_header(key), key)
        for label in preset.labels.values():
            dynamic.setdefault(normalize_header(label), key)
    suggestions, ambiguous, unmatched = suggest_mapping(
        body.headers,
        valid,
        aliases=PERSON_ALIASES,
        fallback_aliases=PERSON_FALLBACK_ALIASES,
        excluded=PERSON_EXCLUDED,
        extra_keys=dynamic,
        ambiguous=PERSON_AMBIGUOUS,
        street_pair_target="residence_address.street",
    )
    return SuggestMappingResponse(suggestions=suggestions, ambiguous=ambiguous, unmatched=unmatched)


@router.post("/company-profiles/suggest-mapping", response_model=SuggestMappingResponse)
async def suggest_company_profiles_mapping(
    body: SuggestMappingRequest, agent: AgentDep, db: DbDep
) -> SuggestMappingResponse:
    from src.client_profiles.profile_sections import (
        COMPANY_PRESET_PROFILE_SECTION,
        COMPANY_TARGET_ALIASES,
    )
    from src.imports.header_aliases import (
        COMPANY_ALIASES,
        COMPANY_EXCLUDED,
        COMPANY_FALLBACK_ALIASES,
        suggest_mapping,
    )
    from src.imports.value_normalizers import ADDRESS_SUBFIELDS

    valid = (
        {"name", "tags"}
        | set(COMPANY_PRESET_PROFILE_SECTION)
        | set(COMPANY_TARGET_ALIASES)
        | {f"{b}.{sub}" for b in ("address", "headquarters_address") for sub in ADDRESS_SUBFIELDS}
    )
    suggestions, ambiguous, unmatched = suggest_mapping(
        body.headers,
        valid,
        aliases=COMPANY_ALIASES,
        fallback_aliases=COMPANY_FALLBACK_ALIASES,
        excluded=COMPANY_EXCLUDED,
        street_pair_target="address.street",
    )
    return SuggestMappingResponse(suggestions=suggestions, ambiguous=ambiguous, unmatched=unmatched)


@router.post("/client-profiles/preview", response_model=ProfileImportPreviewResponse)
async def preview_import_client_profiles(
    body: ProfileImportPreviewRequest, agent: AgentDep, db: DbDep
) -> ProfileImportPreviewResponse:
    """Lot aperçu — le DRY-RUN : la MÊME analyse que l'import réel
    (corrections comprises), zéro écriture, verdicts ligne à ligne
    paginés + récap global."""
    return await ProfileImportManager(db).preview(agent, body)


@router.post("/company-profiles/preview", response_model=CompanyImportPreviewResponse)
async def preview_import_company_profiles(
    body: CompanyImportPreviewRequest, agent: AgentDep, db: DbDep
) -> CompanyImportPreviewResponse:
    return await CompanyImportManager(db).preview(agent, body)


@router.post("/company-profiles", response_model=CompanyImportReport)
async def import_company_profiles(
    body: CompanyImportRequest, agent: AgentDep, db: DbDep
) -> CompanyImportReport:
    """Complément B — import de fiches SOCIÉTÉ : colonnes → dénomination +
    presets de la taxonomie, dédup par dénomination en LIER-pas-dupliquer
    (la logique du 409 souple, en mode import)."""
    return await CompanyImportManager(db).run_import(agent, body)


@router.post("/cases/preview", response_model=ImportPreview)
async def preview_import_cases(
    body: CaseImportRequest, agent: AgentDep, db: DbDep
) -> ImportPreview:
    """Dry-run: validate the CSV + mapping and report each row's PREDICTED
    outcome WITHOUT creating any dossier or queuing any email — no
    BackgroundTasks, no commit, strictly read-only."""
    return await CaseImportManager(db).preview_import(agent, body)


# --- saved mappings (BLOC 3) ---------------------------------------------------------


@router.get("/mappings", response_model=MappingListResponse)
async def list_mappings(
    agent: AgentDep,
    db: DbDep,
    journey_template_id: uuid.UUID | None = None,
    crm_slug: str | None = None,
) -> MappingListResponse:
    return await MappingManager(db).list(
        agent, journey_template_id=journey_template_id, crm_slug=crm_slug
    )


@router.get("/mappings/resolve", response_model=MappingResponse)
async def resolve_mapping(
    agent: AgentDep,
    db: DbDep,
    journey_template_id: uuid.UUID,
    crm_slug: str,
) -> MappingResponse:
    """The applicable mapping for (parcours, crm) — to pre-fill the import."""
    return await MappingManager(db).resolve(agent, journey_template_id, crm_slug)


@router.post("/mappings", response_model=MappingResponse)
async def upsert_mapping(body: MappingUpsertRequest, agent: AgentDep, db: DbDep) -> MappingResponse:
    return await MappingManager(db).upsert(agent, body)


@router.delete("/mappings/{mapping_id}", status_code=204)
async def delete_mapping(mapping_id: uuid.UUID, agent: AgentDep, db: DbDep) -> None:
    await MappingManager(db).delete(agent, mapping_id)

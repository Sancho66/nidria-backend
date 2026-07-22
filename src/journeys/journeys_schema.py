import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_serializer

from src.core.currencies import CurrencyCode
from src.core.enums import (
    AgencySector,
    CompletionMode,
    ResponsibleType,
    StepParticipantRole,
    StepRequirementKind,
    StepRequirementScope,
    StepValidatorType,
)
from src.core.i18n import Language

# BLOC 2bis — i18n WRITE: optional {lang: text} blob accepted alongside (or
# instead of) the scalar. Empty values are dropped (absent key, never ""); the
# manager keeps the scalar in sync via apply_i18n_write.
I18nBlob = dict[str, str]


class JourneyImportRequest(BaseModel):
    """POST /journeys/import — the ROOT JSON produced by the agency's own
    AI ({version, parcours}). `parcours` is validated by hand in the
    import manager (never by pydantic) so every violation surfaces as an
    import_ai.* catalog code with {chemin, valeur} params the front can
    render in the agency's language.

    `provider_assignments` (point 6 Eric) resolves external SLOTS: a map
    {slot_job: {type, id}} — POLYMORPHIC: type='agent' names an is_external
    Agent (account), type='external' names a directory external_contact
    (no account, exactly as assignable). Optional (without it the slots stay
    reporting-only). An assigned job becomes a real participant on its steps
    and disappears from the preview's external_slots."""

    version: int = 1
    parcours: dict[str, Any]
    provider_assignments: dict[str, "ProviderRef"] | None = None


class ProviderRef(BaseModel):
    """A polymorphic provider reference in provider_assignments: an is_external
    Agent (account) OR a directory external_contact (no account)."""

    type: Literal["agent", "external"]
    id: uuid.UUID


class ImportStepCreated(BaseModel):
    """One step created (or, in preview, that WOULD be created)."""

    ref: str
    name: str
    position: int
    fields: int  # informations_a_collecter kept on this step


class ImportStepIgnored(BaseModel):
    """One step rejected by validation (partial import) with the exact
    reason, front-renderable via the code catalog."""

    ref: str | None
    code: str
    chemin: str
    valeur: str | None = None


class ImportWarningItem(BaseModel):
    """Non-blocking notice (ignored volet A, suspected personal data,
    participants to finalize...)."""

    code: str
    chemin: str | None = None
    valeur: str | None = None


class AssignableProvider(BaseModel):
    """A provider of the agency the front can pick to fill a slot —
    POLYMORPHIC: type='agent' (an is_external Agent, an ACCOUNT provider) or
    type='external' (a directory external_contact with NO account, assignable
    exactly the same). `id` is the agent id or the contact id per type; `role`
    is the métier hint (the external role name, or the contact type)."""

    type: Literal["agent", "external"]
    id: uuid.UUID
    name: str
    role: str | None = None


class ImportExternalSlot(BaseModel):
    """A typed external-provider SLOT ('prestataire:<job>') to be named
    by the agency: the template participant model requires a real
    provider identity, so the import never invents one — the slot lists
    the steps waiting for the provider of that job, plus the agency's
    assignable providers (the front proposes a choice). A slot that has
    a provider_assignment is resolved and no longer listed."""

    job: str
    steps: list[str]  # step refs
    assignable: list[AssignableProvider] = []


class ImportParticipantsSummary(BaseModel):
    client: int
    agency: int
    external_slots: list[ImportExternalSlot]


class JourneyImportReport(BaseModel):
    """The import outcome, same shape in preview and creation —
    `template_id` is only set when the journey was actually created."""

    template_id: uuid.UUID | None
    name: str
    created: bool
    steps_created: list[ImportStepCreated]
    steps_ignored: list[ImportStepIgnored]
    participants: ImportParticipantsSummary
    warnings: list[ImportWarningItem]


class JourneyTranslateRequest(BaseModel):
    """POST /journeys/{id}/translate — empty body = every incomplete
    language. The source language is never a valid target.
    `include_stale=True` also RE-translates (overwrites) the AI variants
    whose source drifted; human translations and human corrections are
    never touched, in any mode. `retranslate_langs` is the CONSENTED
    overwrite: for those languages EVERY field of the template is
    regenerated — including human work — and the hash trail is laid, so
    staleness detection works afterwards on pre-feature translations.
    Never implied; the front confirms explicitly before sending it."""

    target_langs: list[Language] | None = None
    include_stale: bool = False
    retranslate_langs: list[Language] | None = None


class LangTranslationCounts(BaseModel):
    """Per-language field counts: `empty` variants to fill, `stale` AI
    variants whose source drifted (human work never counts here)."""

    empty: int
    stale: int


class TranslateEstimateResponse(BaseModel):
    """GET /journeys/{id}/translate/estimate — the front's honest number
    (calibrated on real runs) plus the quota state. `counts` carries the
    per-language {empty, stale} split in BOTH modes, so the modal can
    offer the retranslate choice instead of a flat 'already complete'."""

    items: int
    langs: list[str]
    counts: dict[str, LangTranslationCounts]
    estimated_points: int
    quota_used: int
    quota_limit: int
    month: str


class JobProgress(BaseModel):
    done: int
    total: int


class TranslationJobResponse(BaseModel):
    """An async translation job — progress.done/progress.total IS the
    progress bar; poll GET /journeys/translate-jobs/{id} until
    done|done_with_gaps|failed. One lot = one language."""

    id: uuid.UUID
    translation_job_id: uuid.UUID  # alias of id (the ticket's name)
    template_id: uuid.UUID
    # done = 0 residue ; done_with_gaps = good fields written, some keys
    # need manual review (see failed_keys) ; failed = nothing written
    # (parsing/network/upstream — a true failure).
    status: str  # pending | running | done | done_with_gaps | failed
    langs: list[str]
    progress: JobProgress
    translated_keys: int
    points_charged: int
    error: str | None
    # "{lang}:{content_key}" of the keys to review; empty unless done_with_gaps.
    failed_keys: list[str] = []
    created_at: datetime
    updated_at: datetime


class JourneyTemplateCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    name_i18n: I18nBlob | None = None


class JourneyTemplateUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    name_i18n: I18nBlob | None = None
    # Point 6c — editor preference only (no resolution impact). Validated
    # against SUPPORTED_LANGUAGES in the manager (catalogue error code);
    # exclude_unset distinguishes "untouched" from an explicit null reset.
    editing_language: str | None = Field(default=None, max_length=5)


class TemplateStepCreateRequest(BaseModel):
    """No `position`: new steps are APPENDED; ordering is managed only
    through the declarative reorder endpoint.

    extra="forbid" (BUG-A, 2026-07-19): participants ("Action à réaliser
    par") are a SUB-RESOURCE (/participants), not a step field — an inline
    unknown key used to be silently swallowed (200, nothing written); now
    it 422s loudly."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    name_i18n: I18nBlob | None = None
    estimated_days: int | None = Field(default=None, ge=0)
    default_responsible_type: ResponsibleType | None = None
    # Wave C legacy: a named default responsible — INTERNAL agent only, kept
    # for compatibility (the editor now names "who does the step" via
    # participants, incl. type=external for a no-account provider).
    default_responsible_agent_id: uuid.UUID | None = None
    # "Action validée par" (refonte). `validated_by_type` supersedes
    # `completion_mode`; both are accepted during the transition and the
    # manager keeps them coherent (none⇄auto, else⇄agency_validation).
    # `default_validated_by_agent_id` = the named validator (internal member
    # or durable provider), like default_responsible_agent_id.
    completion_mode: CompletionMode | None = None
    validated_by_type: StepValidatorType | None = None
    default_validated_by_agent_id: uuid.UUID | None = None


class TemplateStepUpdateRequest(BaseModel):
    # Same forbid rationale as TemplateStepCreateRequest (BUG-A).
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=200)
    name_i18n: I18nBlob | None = None
    estimated_days: int | None = Field(default=None, ge=0)
    default_responsible_type: ResponsibleType | None = None
    default_responsible_agent_id: uuid.UUID | None = None
    completion_mode: CompletionMode | None = None
    validated_by_type: StepValidatorType | None = None
    default_validated_by_agent_id: uuid.UUID | None = None
    # Feature 2 — descending agency note (null clears). Partial PATCH:
    # only applied when the key is present.
    content_note: str | None = Field(default=None, max_length=5000)
    content_note_i18n: I18nBlob | None = None


class StepAttachmentResponse(BaseModel):
    """A file the agency attached to a template step (Feature 2). The
    bytes are fetched via the dedicated download endpoint, not inlined."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    step_id: uuid.UUID
    filename: str
    position: int


class TemplateStepParticipantResponse(BaseModel):
    """A template participant ("Action à réaliser par", N). type ∈ {expat,
    agent, external}. For agent/expat the editor resolves the name client-side
    (member lists); for `external` the server resolves the directory contact
    name (`name`), since a contact is not in the member lists."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    type: str
    agent_id: uuid.UUID | None
    external_id: uuid.UUID | None = None
    name: str | None = None  # resolved contact name for type=external
    role: str


class StepParticipantCreateRequest(BaseModel):
    """Add a participant on a template step. Exactly one person id, coherent
    with `type`: `expat` ⟹ neither; `agent` ⟹ agent_id (or NULL = the agency
    in general); `external` ⟹ external_id (a directory contact). agent_id AND
    external_id together → 422 (never a raw 500). `role` cannot be `validator`."""

    type: ResponsibleType
    agent_id: uuid.UUID | None = None
    external_id: uuid.UUID | None = None
    role: StepParticipantRole


class StepRequirementCreateRequest(BaseModel):
    kind: StepRequirementKind
    reference: str = Field(min_length=1, max_length=100)
    scope: StepRequirementScope
    position: int = 0


class StepRequirementResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    step_id: uuid.UUID
    kind: str
    reference: str
    scope: str
    position: int


class StepOrderRequest(BaseModel):
    """Full list of the template's step ids in the desired order."""

    step_ids: list[uuid.UUID]


class StepRequirementOrderRequest(BaseModel):
    """Full list of the step's requirement ids in the desired order
    (same convention as StepOrderRequest, one level down)."""

    requirement_ids: list[uuid.UUID]


class RequirementImpactResponse(BaseModel):
    """Impact of deleting a template requirement — read BEFORE the
    destructive delete so the front can confirm strongly ("N dossiers sur
    T ont deja repondu"). An instance is per targeted person, so a single
    case may hold several responses: `cases_with_response` = distinct
    cases with at least one PROVIDED instance, `responses_count` = the
    provided instances themselves, `cases_total` = distinct cases carrying
    the requirement at all (answered or not)."""

    cases_with_response: int
    responses_count: int
    cases_total: int


# --- step CASE requirements (sections chantier, vague C) -----------------------------
# A step may require a client_case column (country/address). Twin of the
# step_requirement schemas, minus kind/scope: a case field has a single
# case-wide value, no person, no scope.


class StepCaseRequirementCreateRequest(BaseModel):
    case_field: str = Field(min_length=1, max_length=30)
    position: int = 0


class StepCaseRequirementResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    step_id: uuid.UUID
    case_field: str
    position: int


class StepCaseRequirementOrderRequest(BaseModel):
    """Full list of the step's case-requirement ids in the desired order."""

    case_requirement_ids: list[uuid.UUID]


class StepPrerequisitesRequest(BaseModel):
    """Declarative: replaces the step's full prerequisite set — the
    whole template graph is re-validated on every mutation."""

    prerequisite_step_ids: list[uuid.UUID]


# --- per-template field collection (NEW WAVE) ----------------------------------------


class TemplateFieldCreateRequest(BaseModel):
    """Attach a field to a template's creation form. `kind` is base_field
    or custom_field (document is a requirement, not a creation field —
    rejected in the manager). `section_id` lets the field be BORN ranged
    (point 5: the CRM-import modal used to mass-create unsectioned
    fields); None keeps the legitimate unsectioned bucket."""

    kind: StepRequirementKind
    reference: str = Field(min_length=1, max_length=100)
    required_at_creation: bool = False
    position: int = 0
    section_id: uuid.UUID | None = None


class TemplateFieldUpdateRequest(BaseModel):
    """Partial PATCH: toggle required_at_creation AND/OR move the field to
    a section. Both optional (exclude_unset distinguishes "not touched"
    from "set to null" for section_id → clearing returns it to the
    unsectioned bucket). Existing callers sending only required_at_creation
    are unaffected."""

    required_at_creation: bool | None = None
    section_id: uuid.UUID | None = None


class TemplateFieldResponse(BaseModel):
    """A template's creation field with its RESOLVED render metadata
    (label/field_type/options for a custom field, batched at read; base
    fields carry none — the frontend knows the civil-status set). A
    custom field whose definition was archived after attachment stays in
    the list, flagged `is_archived` (mirrors requirements)."""

    id: uuid.UUID
    template_id: uuid.UUID
    kind: str
    reference: str
    position: int
    required_at_creation: bool
    label: str | None
    field_type: str | None
    options: list[str] | None
    is_archived: bool
    # Sections chantier (vague A): NULL = unsectioned bucket.
    section_id: uuid.UUID | None


class TemplateFieldOrderRequest(BaseModel):
    """Full list of the template's field ids in the desired order (same
    convention as StepOrderRequest / StepRequirementOrderRequest)."""

    field_ids: list[uuid.UUID]


# --- per-template CASE-field collection (option b) -----------------------------------
# Case-level fields (countries) on client_case — a SEPARATE mechanism from
# the person fields above. Subset of the person-field schema: no kind, no
# resolution, no is_archived (countries are fixed columns, never archived).


class CaseFieldCreateRequest(BaseModel):
    """Attach a case-level field (a client_case column, e.g. a country) to
    a template's creation form. `case_field` is validated against
    COLLECTABLE_CASE_FIELDS in the manager."""

    case_field: str = Field(min_length=1, max_length=30)
    required_at_creation: bool = False
    position: int = 0
    # Born-ranged creation (point 5), mirroring TemplateFieldCreateRequest.
    section_id: uuid.UUID | None = None


class CaseFieldUpdateRequest(BaseModel):
    """Partial PATCH: required_at_creation AND/OR section move (mirrors
    TemplateFieldUpdateRequest)."""

    required_at_creation: bool | None = None
    section_id: uuid.UUID | None = None


class TemplateCaseFieldResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    template_id: uuid.UUID
    case_field: str
    position: int
    required_at_creation: bool
    # Sections chantier (vague A): NULL = unsectioned bucket.
    section_id: uuid.UUID | None


class CaseFieldOrderRequest(BaseModel):
    """Full list of the template's case-field ids in the desired order
    (same convention as TemplateFieldOrderRequest)."""

    case_field_ids: list[uuid.UUID]


# --- sections (sections chantier, vague A) -------------------------------------------


class SectionCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    name_i18n: I18nBlob | None = None
    description: str | None = Field(default=None, max_length=500)
    description_i18n: I18nBlob | None = None


class SectionUpdateRequest(BaseModel):
    """Partial: rename and/or edit the description."""

    name: str | None = Field(default=None, min_length=1, max_length=200)
    name_i18n: I18nBlob | None = None
    description: str | None = Field(default=None, max_length=500)
    description_i18n: I18nBlob | None = None


class SectionOrderRequest(BaseModel):
    """Full list of the template's section ids in the desired order."""

    section_ids: list[uuid.UUID]


class JourneySectionResponse(BaseModel):
    """Section METADATA (CRUD endpoints). The grouped view with fields
    lives in the template detail (JourneySectionDetail)."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    template_id: uuid.UUID
    name: str
    description: str | None
    position: int


class JourneySectionDetail(BaseModel):
    """A section WITH its fields, for the template detail. OPTION 1
    (segmented): person fields then case fields, each in their own
    position order — the response carries two lists, so the segmentation
    is structural."""

    id: uuid.UUID
    name: str
    # BLOC 2bis — RAW i18n blobs for the editor (alongside the resolved
    # `name`/`description` above). Display surfaces keep the resolved value.
    name_i18n: I18nBlob
    description: str | None
    description_i18n: I18nBlob
    position: int
    fields: list["TemplateFieldResponse"]
    case_fields: list["TemplateCaseFieldResponse"]


class UnsectionedFields(BaseModel):
    """The NULL bucket — fields not assigned to any section. Same
    segmented shape, no section identity."""

    fields: list["TemplateFieldResponse"]
    case_fields: list["TemplateCaseFieldResponse"]


class JourneyTemplateResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    # ISO 3166-1 alpha-2 (e.g. "PY") for sample grouping/flag/search; NULL
    # for an ordinary agency template. Flag + localized name are front-side.
    country: str | None = None
    # Business sector of a LIBRARY sample (the 7 sectoral models); NULL for a
    # country sample and for ordinary agency templates. Lets the front split the
    # model library into a "by sector" axis (group + recommend agency.sectors)
    # distinct from the "by country" axis. Read straight off the ORM column.
    sector: AgencySector | None = None


class JourneyCloneRequest(BaseModel):
    """Optional new name for a deep clone; defaults to "{source} (copie)"."""

    name: str | None = Field(default=None, min_length=1, max_length=200)


class PlannedCostCreateRequest(BaseModel):
    """A planned cost line on a template step — same shape as a real cost
    (amount + label + currency). DECIMAL(18,4); the LINE's currency drives
    decimals. `currency` omitted → the agency default; neither → 409."""

    amount: Decimal = Field(max_digits=18, decimal_places=4)
    label: str = Field(min_length=1, max_length=200)
    currency: CurrencyCode | None = None


class PlannedCostUpdateRequest(BaseModel):
    """Partial correction of a template planned cost."""

    amount: Decimal | None = Field(default=None, max_digits=18, decimal_places=4)
    label: str | None = Field(default=None, min_length=1, max_length=200)
    currency: CurrencyCode | None = None


class PlannedCostResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    step_id: uuid.UUID
    amount: Decimal
    currency: str
    label: str
    created_at: datetime
    updated_at: datetime

    # Money as a STRING — never a JSON float (exact to the front).
    @field_serializer("amount")
    def _ser_amount(self, value: Decimal) -> str:
        return str(value)


class TemplateStepResponse(BaseModel):
    id: uuid.UUID
    name: str
    # BLOC 2bis — RAW i18n blob for the editor (alongside the resolved `name`).
    name_i18n: I18nBlob
    position: int
    estimated_days: int | None
    default_responsible_type: str | None
    default_responsible_agent_id: uuid.UUID | None
    completion_mode: str  # kept during the transition (rollback fallback)
    default_validated_by_type: str
    default_validated_by_agent_id: uuid.UUID | None
    # "Action à réaliser par" — N participants (responsible refonte). The
    # legacy default_responsible_* stays for now (transition).
    participants: list[TemplateStepParticipantResponse]
    prerequisite_step_ids: list[uuid.UUID]
    # Feature 2 — descending agency content on the step (template-level).
    content_note: str | None
    content_note_i18n: I18nBlob
    attachments: list[StepAttachmentResponse]
    # Planned costs on this step (agency-internal). Populated ONLY when the
    # agent has cost.view — empty otherwise (an agent editing a journey without
    # cost.view never sees the section). STRUCTURALLY never projected to the
    # expat/external client faces (they read case_step_progress, not templates).
    planned_costs: list[PlannedCostResponse] = []


class CanvasNodePosition(BaseModel):
    x: float
    y: float


class CanvasLayoutRequest(BaseModel):
    """Replace the whole canvas layout blob (MVP-1). Keys are step ids;
    foreign/stale ids are dropped server-side so the blob never rots."""

    positions: dict[uuid.UUID, CanvasNodePosition]


class JourneyTemplateDetailResponse(BaseModel):
    id: uuid.UUID
    name: str
    # BLOC 2bis — RAW i18n blob for the editor (alongside the resolved `name`).
    name_i18n: I18nBlob
    steps: list[TemplateStepResponse]
    # Fields collected at case creation (NEW WAVE) — embedded like steps.
    fields: list[TemplateFieldResponse]
    # Case-level fields (countries) collected at creation (option b) —
    # a SEPARATE list; the UI unifies them with `fields` for display.
    case_fields: list[TemplateCaseFieldResponse]
    # Sections chantier (vague A): the GROUPED view. `fields`/`case_fields`
    # above stay FLAT (all fields, every section) so the existing front
    # keeps working unchanged; `sections` + `unsectioned` are additive.
    sections: list[JourneySectionDetail]
    unsectioned: UnsectionedFields
    # Visual canvas editor (MVP-1): pure-presentation node positions
    # keyed by step id. None = never opened in canvas (front auto-lays-out).
    canvas_layout: dict[str, CanvasNodePosition] | None
    # Usage counters (delete UX): ACTIVE cases block deletion; ARCHIVED cases
    # are auto-detached on delete (their journey history is purged). The front
    # uses these to disable / warn before deleting.
    active_cases_count: int
    archived_cases_count: int
    # Point 6c — editor preference (front-only consumption), NULL = none.
    editing_language: str | None

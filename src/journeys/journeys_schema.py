import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from src.core.enums import (
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
    render in the agency's language."""

    version: int = 1
    parcours: dict[str, Any]


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


class ImportExternalSlot(BaseModel):
    """A typed external-provider SLOT ('prestataire:<job>') to be named
    by the agency: the template participant model requires a real
    provider identity, so the import never invents one — the slot lists
    the steps waiting for the provider of that job."""

    job: str
    steps: list[str]  # step refs


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
    done|failed. One lot = one language."""

    id: uuid.UUID
    translation_job_id: uuid.UUID  # alias of id (the ticket's name)
    template_id: uuid.UUID
    status: str  # pending | running | done | failed
    langs: list[str]
    progress: JobProgress
    translated_keys: int
    points_charged: int
    error: str | None
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
    through the declarative reorder endpoint."""

    name: str = Field(min_length=1, max_length=200)
    name_i18n: I18nBlob | None = None
    estimated_days: int | None = Field(default=None, ge=0)
    default_responsible_type: ResponsibleType | None = None
    # Wave C: a named default responsible — a precise INTERNAL agent only
    # (validated in the manager; externals exist only at the case level).
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
    """A template participant ("Action à réaliser par", N). The editor
    resolves the name client-side from its member lists (like the
    responsible). type ∈ {expat, agent}; role is a StepParticipantRole."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    type: str
    agent_id: uuid.UUID | None
    role: str


class StepParticipantCreateRequest(BaseModel):
    """Add a participant on a template step. `expat` ⟹ no agent; `agent`
    (internal OR durable external) ⟹ agent_id required. `role` cannot be
    `validator` (closed enum — validation stays on the validator field)."""

    type: ResponsibleType  # validated in the manager: external is not a template type
    agent_id: uuid.UUID | None = None
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


class JourneyCloneRequest(BaseModel):
    """Optional new name for a deep clone; defaults to "{source} (copie)"."""

    name: str | None = Field(default=None, min_length=1, max_length=200)


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

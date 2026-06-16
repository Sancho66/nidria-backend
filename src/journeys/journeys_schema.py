import uuid

from pydantic import BaseModel, ConfigDict, Field

from src.core.enums import (
    CompletionMode,
    ResponsibleType,
    StepRequirementKind,
    StepRequirementScope,
)


class JourneyTemplateCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class JourneyTemplateUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)


class TemplateStepCreateRequest(BaseModel):
    """No `position`: new steps are APPENDED; ordering is managed only
    through the declarative reorder endpoint."""

    name: str = Field(min_length=1, max_length=200)
    estimated_days: int | None = Field(default=None, ge=0)
    default_responsible_type: ResponsibleType | None = None
    # Wave C: a named default responsible — a precise INTERNAL agent only
    # (validated in the manager; externals exist only at the case level).
    default_responsible_agent_id: uuid.UUID | None = None
    completion_mode: CompletionMode = CompletionMode.AGENCY_VALIDATION


class TemplateStepUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    estimated_days: int | None = Field(default=None, ge=0)
    default_responsible_type: ResponsibleType | None = None
    default_responsible_agent_id: uuid.UUID | None = None
    completion_mode: CompletionMode | None = None


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
    rejected in the manager)."""

    kind: StepRequirementKind
    reference: str = Field(min_length=1, max_length=100)
    required_at_creation: bool = False
    position: int = 0


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
    description: str | None = Field(default=None, max_length=500)


class SectionUpdateRequest(BaseModel):
    """Partial: rename and/or edit the description."""

    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=500)


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
    description: str | None
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


class TemplateStepResponse(BaseModel):
    id: uuid.UUID
    name: str
    position: int
    estimated_days: int | None
    default_responsible_type: str | None
    default_responsible_agent_id: uuid.UUID | None
    completion_mode: str
    prerequisite_step_ids: list[uuid.UUID]


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

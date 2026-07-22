import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_serializer

from src.cases.filter_schema import AdvancedFilters
from src.core.currencies import CurrencyCode
from src.core.email import NormalizedEmailStr
from src.core.enums import (
    CaseStatus,
    CaseUrgency,
    ContactChannel,
    ExternalContactType,
    MaritalStatus,
    Sex,
)
from src.progress.progress_schema import StepProgressResponse

_COUNTRY_PATTERN = r"^[A-Z]{2}$"


class _CivilStatusFields(BaseModel):
    """Case-scoped civil/professional status — all optional, never on
    expat_user."""

    passport_number: str | None = Field(default=None, max_length=50)
    date_of_birth: date | None = None
    nationality: str | None = Field(default=None, max_length=100)
    place_of_birth: str | None = Field(default=None, max_length=200)
    sex: Sex | None = None
    marital_status: MaritalStatus | None = None
    phone: str | None = Field(default=None, max_length=50)
    # Preferred contact channels (display/preference only — reminders stay
    # email-only, see reminders_jobs). phone/whatsapp reuse `phone`.
    preferred_channels: list[ContactChannel] | None = None
    birth_name: str | None = Field(default=None, max_length=200)
    profession: str | None = Field(default=None, max_length=200)
    employer: str | None = Field(default=None, max_length=200)


class CaseCreateRequest(_CivilStatusFields):
    """Principal-only create: family members and external contacts go
    through their own endpoints.

    `journey_template_id` is REQUIRED (product decision 2026-07-11): a case
    without a journey is an empty shell — no steps, no timeline, nothing in
    the worklist, an empty client space. "I'll assign later" produced dead
    dossiers. Missing → an explicit 422. The CRM import composes this same
    schema and always injects its (already mandatory) template per row —
    exempt by construction; existing journey-less cases keep assign_journey."""

    # Principal expat (linked-or-created by email; an EXISTING user's
    # identity is never overwritten by this payload).
    first_name: str = Field(min_length=1, max_length=100)
    last_name: str = Field(min_length=1, max_length=100)
    email: NormalizedEmailStr
    preferred_lang: str = Field(default="fr", min_length=2, max_length=5)
    # Case — origin/destination addresses (flat columns on client_case).
    # country stays separate (its query ecosystem); street/city/postal are
    # collectable address fields added in the sections chantier (vague B).
    origin_country: str | None = Field(default=None, pattern=_COUNTRY_PATTERN)
    origin_street: str | None = Field(default=None, max_length=255)
    origin_city: str | None = Field(default=None, max_length=100)
    origin_postal_code: str | None = Field(default=None, max_length=20)
    dest_country: str | None = Field(default=None, pattern=_COUNTRY_PATTERN)
    dest_street: str | None = Field(default=None, max_length=255)
    dest_city: str | None = Field(default=None, max_length=100)
    dest_postal_code: str | None = Field(default=None, max_length=20)
    status: CaseStatus = CaseStatus.PROSPECT
    source: str | None = Field(default=None, max_length=100)
    # Agency's internal case reference (free text, not unique).
    reference: str | None = Field(default=None, max_length=100)
    tags: list[str] = Field(default_factory=list)
    owner_agent_id: uuid.UUID | None = None  # default: the creator
    # REQUIRED — see the class docstring (a journey-less case is a dead shell).
    journey_template_id: uuid.UUID
    custom_fields: dict[str, Any] = Field(default_factory=dict)
    # Opt-in prefill: copy the PERSON data (principal + family, civil +
    # custom fields) from a previous dossier of the SAME client in the
    # SAME agency. The new case itself starts fresh (no journey/steps/
    # documents/tags/status/notes), and wizard-provided fields WIN.
    prefill_from_case_id: uuid.UUID | None = None
    # Billed price (optional at creation; cost.manage enforced in the
    # manager). Currency omitted → the agency default; neither → 409.
    billed_amount: Decimal | None = Field(default=None, max_digits=18, decimal_places=4)
    billed_currency: CurrencyCode | None = None


class PrefillSourceResponse(BaseModel):
    """A prefill candidate: one of the client's dossiers in MY agency
    (creation wizard picker). Demo and deleted cases never appear."""

    id: uuid.UUID
    journey_name: str | None
    created_at: datetime


class CaseUpdateRequest(BaseModel):
    origin_country: str | None = Field(default=None, pattern=_COUNTRY_PATTERN)
    origin_street: str | None = Field(default=None, max_length=255)
    origin_city: str | None = Field(default=None, max_length=100)
    origin_postal_code: str | None = Field(default=None, max_length=20)
    dest_country: str | None = Field(default=None, pattern=_COUNTRY_PATTERN)
    dest_street: str | None = Field(default=None, max_length=255)
    dest_city: str | None = Field(default=None, max_length=100)
    dest_postal_code: str | None = Field(default=None, max_length=20)
    status: CaseStatus | None = None
    source: str | None = Field(default=None, max_length=100)
    reference: str | None = Field(default=None, max_length=100)
    tags: list[str] | None = None
    owner_agent_id: uuid.UUID | None = None
    # Billed price (cost.manage enforced in the manager). exclude_unset
    # semantics: billed_amount=null CLEARS the price (both fields);
    # billed_currency alone re-denominates an existing price.
    billed_amount: Decimal | None = Field(default=None, max_digits=18, decimal_places=4)
    billed_currency: CurrencyCode | None = None


class CaseResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    agency_id: uuid.UUID
    principal_expat_user_id: uuid.UUID
    owner_agent_id: uuid.UUID | None
    journey_template_id: uuid.UUID | None
    # Addresses, flat. origin_country/dest_country unchanged (filters
    # /sorts/views read them); street/city/postal_code are the additions.
    origin_country: str | None
    origin_street: str | None
    origin_city: str | None
    origin_postal_code: str | None
    dest_country: str | None
    dest_street: str | None
    dest_city: str | None
    dest_postal_code: str | None
    status: str
    source: str | None
    reference: str | None
    tags: list[str]
    created_at: datetime
    updated_at: datetime


class PrincipalSummaryResponse(BaseModel):
    """Identity subset for list items — the full PrincipalResponse
    (activated) stays a detail-endpoint concern."""

    model_config = ConfigDict(from_attributes=True)

    first_name: str
    last_name: str
    email: str
    preferred_lang: str


class CaseListItemResponse(CaseResponse):
    principal: PrincipalSummaryResponse
    # Resolved journey name (resolve_i18n of the template's name for the
    # request language) — NULL when the case has no journey (e.g. archived &
    # detached). journey_template_id stays above for a possible link.
    journey_name: str | None = None
    # Current step (cohérence statut/étape): the FIRST non-validated step
    # in journey order (the progression-band rule), name resolved for the
    # request language, position as "3/7". Both NULL without a journey or
    # when everything is validated.
    current_step_name: str | None = None
    current_step_position: str | None = None
    # Derived urgency (same rule as the dashboard worklist, lifted to the case
    # — see src/cases/case_urgency.py): overdue > to_validate > awaiting_client
    # > neutral. Sortable (?sort_by=urgency) and filterable (?urgency=…).
    urgency: CaseUrgency = CaseUrgency.NEUTRAL


class CaseListResponse(BaseModel):
    items: list[CaseListItemResponse]
    total: int
    page: int
    page_size: int


class PersonResponse(_CivilStatusFields):
    """A person on the case — PRINCIPAL or FAMILY, one homogeneous shape.
    PRINCIPAL: identity (first/last/email/lang) resolved from the shared
    expat_user (full_name NULL); FAMILY: carries full_name, identity
    fields NULL."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    kind: str
    relationship: str | None
    full_name: str | None
    # PATCH email, etat (b) : l'invitation a ete renvoyee a la nouvelle
    # adresse (l'ancienne est annulee, son token mort) — le front informe.
    invitation_resent: bool = False
    # Resolved from expat_user for PRINCIPAL (so the frontend shows the
    # name without a second fetch); NULL for FAMILY.
    expat_user_id: uuid.UUID | None
    first_name: str | None
    last_name: str | None
    email: str | None
    preferred_lang: str | None
    activated: bool | None
    # Agency custom-field values — only keys with an ACTIVE definition.
    custom_fields: dict[str, Any]


class PersonCreateRequest(_CivilStatusFields):
    """Creates a FAMILY member (the PRINCIPAL exists with the case).

    `email` is OPTIONAL: when given, the agency invites the member — the
    member gets a read-only account (linked-or-created expat_user by email,
    the same global pivot as the principal) and sees the dossier's progress
    plus their OWN fields/documents. When absent, the member stays what they
    are today: a person on the dossier with no login. Nothing changes for them.
    """

    full_name: str = Field(min_length=1, max_length=200)
    relationship: str = Field(min_length=1, max_length=50)
    email: NormalizedEmailStr | None = None
    custom_fields: dict[str, Any] = Field(default_factory=dict)


class PersonUpdateRequest(_CivilStatusFields):
    """Edits any person. For FAMILY, full_name/relationship are editable;
    for PRINCIPAL they are ignored (its name lives on expat_user).
    `custom_fields` is a partial MERGE keyed by definition key.

    `email` (Arthur): on a person WITHOUT an account, links (or creates) the
    read-only member access — exactly the creation semantics. On a person who
    ALREADY has one, a different email is a 409 (remove the access, then
    re-invite); empty ("" or null) or identical → clean no-op."""

    full_name: str | None = Field(default=None, min_length=1, max_length=200)
    relationship: str | None = Field(default=None, min_length=1, max_length=50)
    email: NormalizedEmailStr | Literal[""] | None = None
    custom_fields: dict[str, Any] | None = None


class ExternalContactCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    email: EmailStr | None = None
    phone: str | None = Field(default=None, max_length=50)
    type: ExternalContactType = ExternalContactType.OTHER


class ExternalContactUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    email: EmailStr | None = None
    phone: str | None = Field(default=None, max_length=50)
    type: ExternalContactType | None = None


class ExternalContactResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    email: str | None
    phone: str | None
    type: str


class CaseNoteCreateRequest(BaseModel):
    body: str = Field(min_length=1)
    is_confidential: bool = False


class CaseNoteUpdateRequest(BaseModel):
    """`is_confidential` is immutable after creation — flipping it
    would silently move the note across the visibility boundary."""

    body: str = Field(min_length=1)


class CaseNoteResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    author_agent_id: uuid.UUID | None
    body: str
    is_confidential: bool
    created_at: datetime
    updated_at: datetime


class CustomFieldDefinitionInline(BaseModel):
    """Active custom-field definitions, embedded in the case detail so
    the frontend renders the person form in one fetch (no second call /
    no cross-case cache staleness)."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    key: str
    label: str
    field_type: str
    options: list[str] | None
    required: bool
    position: int


class CaseBillingInfo(BaseModel):
    """The dossier's billed price + margin, agent face ONLY, cost.view-gated
    at the MANAGER (the block is entirely ABSENT from the payload without the
    permission — not null-with-a-hint). Money as strings, never a JSON float;
    `margin` is SERVED (billed − real costs), only when every real cost shares
    the price's currency — otherwise null with the reason."""

    billed_amount: Decimal | None
    billed_currency: str | None
    margin: Decimal | None
    margin_unavailable_reason: Literal["mixed_currencies"] | None

    @field_serializer("billed_amount", "margin")
    def _ser_money(self, value: Decimal | None) -> str | None:
        return str(value) if value is not None else None


class CaseDetailResponse(CaseResponse):
    # Resolved journey name (same rule as the list); NULL when no journey.
    journey_name: str | None = None
    # Current step, same rule as the list (both NULL without a journey or
    # all validated).
    current_step_name: str | None = None
    current_step_position: str | None = None
    # Billed price + margin — present (an object) ONLY for a cost.view
    # holder. The KEY is dropped entirely for the others AT THE ROUTER (the
    # single endpoint returning this model): a @model_serializer here would
    # blind the openapi export — Pydantic derives the serialization schema
    # from the serializer's return annotation, and CaseDetailResponse
    # exported as {} (the 2026-07-11 front bug). Never re-add one.
    billing: CaseBillingInfo | None = None
    # Unified list: principal (kind=principal) + family, one shape. The
    # principal is findable in O(1) via principal_person_id (invariant:
    # exactly one kind=principal person per case).
    persons: list[PersonResponse]
    principal_person_id: uuid.UUID
    # Agency's ACTIVE custom-field definitions (form schema).
    custom_field_definitions: list[CustomFieldDefinitionInline]
    external_contacts: list[ExternalContactResponse]
    notes: list[CaseNoteResponse]
    # Projected timeline (BLOCKED computed at read time).
    progress: list[StepProgressResponse]


class CaseFilters(BaseModel):
    """Query-side contract of GET /cases."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    status: list[CaseStatus] | None = None
    # Derived urgency filter (OR within, AND-combined with `status`).
    urgency: list[CaseUrgency] | None = None
    origin_country: str | None = Field(default=None, pattern=_COUNTRY_PATTERN)
    dest_country: str | None = Field(default=None, pattern=_COUNTRY_PATTERN)
    owner_agent_id: uuid.UUID | None = None
    preferred_lang: str | None = None
    tag: list[str] | None = None  # contains-ALL semantics
    q: str | None = None  # ilike on principal first/last name + email
    # Parsed AdvancedFilters tree (the `filters` query param, Prism
    # filter bar) — AND-combined with the per-field filters above.
    advanced: AdvancedFilters | None = None

    def as_dict(self) -> dict[str, Any]:
        data = self.model_dump(exclude_none=True, exclude={"advanced"})
        if self.advanced is not None:
            data["advanced"] = self.advanced
        return data


# --- bulk actions --------------------------------------------------------------

# Hard cap on a single bulk call — a sane guard-rail, not a Prism port.
_BULK_MAX_IDS = 500


class _BulkBase(BaseModel):
    case_ids: list[uuid.UUID] = Field(min_length=1, max_length=_BULK_MAX_IDS)


class BulkSetStatusRequest(_BulkBase):
    action: Literal["set_status"] = "set_status"
    status: CaseStatus


class BulkSetOwnerRequest(_BulkBase):
    action: Literal["set_owner"] = "set_owner"
    # null = unassign (mirrors the SET NULL FK and the unit PATCH).
    owner_agent_id: uuid.UUID | None = None


class BulkAddTagsRequest(_BulkBase):
    action: Literal["add_tags"] = "add_tags"
    tags: list[str] = Field(min_length=1)


class BulkRemoveTagsRequest(_BulkBase):
    action: Literal["remove_tags"] = "remove_tags"
    tags: list[str] = Field(min_length=1)


# Discriminated union: the `action` field routes to the right shape, so
# a set_status payload missing `status` is a 422, not a silent no-op.
BulkActionRequest = Annotated[
    BulkSetStatusRequest | BulkSetOwnerRequest | BulkAddTagsRequest | BulkRemoveTagsRequest,
    Field(discriminator="action"),
]


class BulkDeleteRequest(_BulkBase):
    """Soft delete (case.delete). Separate route from bulk-action so the
    RBAC engine gates each on its own permission (Prism splits these too)."""


class BulkActionResponse(BaseModel):
    """`examined` = ids submitted; `affected` = rows actually changed
    (own-agency, mutation applied); `affected_ids` lets the frontend
    refresh + deselect. examined − affected reveals ignored ids
    (cross-agency, already-deleted, no-op)."""

    action: str
    examined: int
    affected: int
    affected_ids: list[uuid.UUID]

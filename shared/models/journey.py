import uuid
from typing import Any

from sqlalchemy import CheckConstraint, ForeignKey, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from shared.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class JourneyTemplate(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Reusable journey MODEL. Owned by an agency (`agency_id` set), OR a
    shared LIBRARY sample (`agency_id IS NULL` + `is_sample=true`) — same
    pattern as a system `role` (agency_id NULL + is_system). A sample is
    READ-ONLY for agencies: every write path resolves via
    get_template_in_agency (WHERE agency_id == me), and `NULL = <agency>` is
    never true in SQL → a sample is unreachable for list/edit/delete/assign.
    The agency consumes a sample only by CLONING it (later block).
    Its instantiation on a case is `case_step_progress`."""

    __tablename__ = "journey_template"
    __table_args__ = (
        # Point 6c — same referential as agency.default_language (NULL passes).
        CheckConstraint(
            "editing_language IN ('fr', 'en', 'es', 'ru', 'pt', 'it')",
            name="journey_template_editing_language_check",
        ),
    )

    # NULL ⟺ library sample (is_sample=true); otherwise the owning agency.
    agency_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("agency.id", ondelete="CASCADE"), index=True
    )
    # Library sample (shared, agency-less). Mirrors role.is_system.
    is_sample: Mapped[bool] = mapped_column(
        default=False, server_default=text("false"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    # i18n — parallel {lang: text} blob for the name. The scalar `name` stays
    # the read fallback AND the seed's idempotence anchor (never keyed on the
    # blob). Absent language = absent key.
    name_i18n: Mapped[dict[str, str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )
    # ISO 3166-1 alpha-2 country code (e.g. "PY") — for grouping / flag /
    # search of samples. NULLABLE: an ordinary agency template may have none;
    # a sample carries one. No country table/referential — the flag + the
    # localized name are a FRONT concern (Intl.DisplayNames from the code).
    country: Mapped[str | None] = mapped_column(String(2))
    # Visual canvas editor (MVP-1): pure-presentation node positions,
    # { "<step_id>": {"x": float, "y": float} }. NULL = never opened in
    # canvas (the front auto-lays-out with dagre). Never affects journey
    # logic — droppable without consequence. A separate presentation layer.
    canvas_layout: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    # Point 6c — the EDITOR's default language for THIS template: a pure
    # editing convenience consumed by the FRONT (pre-selects the language
    # tab in the step/section/field editors). Read by NO backend
    # resolution path: client-facing resolution stays client language →
    # agency default → fr, notifications untouched. NULL = no preference.
    editing_language: Mapped[str | None] = mapped_column(String(5))


class JourneySection(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A freely-named, ordered GROUP of creation fields on a template
    (sections chantier). PLANE-AGNOSTIC: a section may hold person fields
    (journey_template_field) AND case fields (journey_template_case_field)
    — the unification is presentational, the storage stays two planes.

    Fields reference a section via a NULLABLE section_id (SET NULL on
    delete: removing a section never destroys a field declaration; its
    fields fall back to the NULL bucket). `name` is free; no uniqueness
    (a label, not an identifier).

    `seed_key` (samples phase B): the STABLE anchor of a seeded section
    on a library sample — one of the 11 section-type keys. NULL on every
    agency-made section; the seed reconciles by this key, never by name
    (the dash-purge lesson)."""

    __tablename__ = "journey_section"
    __table_args__ = (UniqueConstraint("template_id", "seed_key", name="uq_section_seed_key"),)

    template_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("journey_template.id", ondelete="CASCADE"), index=True, nullable=False
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(String(500))
    seed_key: Mapped[str | None] = mapped_column(String(60))
    # BLOC 1 i18n — parallel {lang: text} blobs. The scalar columns above stay
    # the read source until BLOC 2 switches resolution. Absent language = absent
    # key (never an empty string). NOT NULL with a '{}' default.
    name_i18n: Mapped[dict[str, str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )
    description_i18n: Mapped[dict[str, str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )
    position: Mapped[int] = mapped_column(default=0, nullable=False)


class JourneyTemplateStep(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "journey_template_step"
    __table_args__ = (UniqueConstraint("template_id", "position"),)

    template_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("journey_template.id", ondelete="CASCADE"), index=True, nullable=False
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    position: Mapped[int] = mapped_column(nullable=False)
    estimated_days: Mapped[int | None] = mapped_column()
    default_responsible_type: Mapped[str | None] = mapped_column(String(20))
    # Optional NAMED default responsible (wave C): a precise INTERNAL agent
    # (durable — externals exist only at the case level, never on the
    # generic template; the Manager enforces is_external=False). Copied to
    # the progress row at journey assignment.
    default_responsible_agent_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("agent.id", ondelete="SET NULL")
    )
    # How the step closes (NEW WAVE). Default reproduces the current
    # flow (agency closes the step) — additive, breaks nothing. `auto`
    # is a capability flag here; the active auto-complete trigger lands
    # in a later wave.
    completion_mode: Mapped[str] = mapped_column(
        String(20),
        default="agency_validation",
        server_default=text("'agency_validation'"),
        nullable=False,
    )
    # "Action validée par" (refonte) — the validator TYPE lives on the
    # template (config), the precise person on the instance (case_step_
    # progress), exactly like the responsible. Default 'agent' = the agency
    # validates (= the former completion_mode default 'agency_validation').
    # `completion_mode` is kept in sync during the transition (rollback-safe).
    default_validated_by_type: Mapped[str] = mapped_column(
        String(20),
        default="agent",
        server_default=text("'agent'"),
        nullable=False,
    )
    # Optional NAMED default validator: an internal member OR a durable
    # external provider (resolved is_external → type 'external' on the
    # instance), copied to the progress row at assignment. NULL = "the
    # agency in general" (any member validates) for type 'agent'.
    default_validated_by_agent_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("agent.id", ondelete="SET NULL")
    )
    # Feature 2 — DESCENDING content the agency provides on the step
    # (a note/instruction), distinct from step requirements (which ASK the
    # client). Lives on the TEMPLATE → the same for every case of this
    # journey. Attachments are in journey_step_attachment.
    content_note: Mapped[str | None] = mapped_column(Text)
    # BLOC 1 i18n — parallel {lang: text} blobs for name + content_note. The
    # scalar columns stay the read source until BLOC 2 switches resolution.
    # Absent language = absent key (never an empty string).
    name_i18n: Mapped[dict[str, str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )
    content_note_i18n: Mapped[dict[str, str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )


class JourneyStepAttachment(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A file the agency attaches to a TEMPLATE step (Feature 2 — descending
    content). Stored in Supabase Storage (the generic storage primitive,
    NOT the case-scoped `document` table): the file lives on the template,
    shared by every case of this journey. Read access is audience-filtered
    at the projection layer (agency: always; expat: read-only on its case
    timeline; external: only on steps where it is the responsible — V2)."""

    __tablename__ = "journey_step_attachment"

    step_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("journey_template_step.id", ondelete="CASCADE"), index=True, nullable=False
    )
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    storage_path: Mapped[str] = mapped_column(String(500), nullable=False)
    uploaded_by_agent_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("agent.id", ondelete="SET NULL")
    )
    position: Mapped[int] = mapped_column(default=0, nullable=False)


class JourneyStepParticipant(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """ "Action à réaliser par" at the TEMPLATE level (responsible refonte:
    1 → N participants with a role). Snapshot-copied to each case at
    assignment (like the responsible), so editing it never retro-changes a
    live dossier. Polymorphic person calqué sur le responsable: {expat, agent,
    external}. An `external` participant names a DIRECTORY external_contact
    (agency-scoped, case_id NULL) — a durable provider that may never have an
    account; it propagates to every case by REFERENCE at assignment. (This
    supersedes the old "externals exist only at the case level" rule.) The
    `validator` role is NOT here: validation stays on validated_by_*."""

    __tablename__ = "journey_step_participant"
    __table_args__ = (
        # Polymorphic person: expat ⟹ no agent; agent ⟹ agent_id OPTIONAL —
        # Polymorphic person, CALQUÉ on the instance participant: exactly one
        # of agent_id / external_id, coherent with type. agent ⟹ agent_id
        # OPTIONAL (NULL = "the agency in general"); external ⟹ a directory
        # external_contact — a NAMED provider that may never have an account
        # (the case a template must express: Nicolas's notary, named once on
        # the journey, no login). This WIDENS the former "externals exist only
        # at the case level" rule, which a durable no-account provider invalidates.
        CheckConstraint(
            "(type = 'agent' AND external_id IS NULL)"
            " OR (type = 'expat' AND agent_id IS NULL AND external_id IS NULL)"
            " OR (type = 'external' AND external_id IS NOT NULL AND agent_id IS NULL)",
            name="participant_template_type_matches_fk",
        ),
    )

    step_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("journey_template_step.id", ondelete="CASCADE"), index=True, nullable=False
    )
    type: Mapped[str] = mapped_column(String(20), nullable=False)  # expat | agent | external
    agent_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("agent.id", ondelete="SET NULL"))
    external_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("external_contact.id", ondelete="SET NULL")
    )
    role: Mapped[str] = mapped_column(String(30), nullable=False)  # StepParticipantRole


class StepPrerequisite(Base):
    """Self-referencing M2M between steps of the SAME template
    (locked-steps feature). Same-template + no-cycle validation is
    applicative (step 8); the DB only rules out self-reference."""

    __tablename__ = "step_prerequisite"
    __table_args__ = (
        CheckConstraint("step_id != prerequisite_step_id", name="no_self_prerequisite"),
    )

    step_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("journey_template_step.id", ondelete="CASCADE"), primary_key=True
    )
    prerequisite_step_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("journey_template_step.id", ondelete="CASCADE"), primary_key=True
    )


class JourneyTemplateField(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A field a template COLLECTS at case creation (NEW WAVE) — the
    explicit list driving the dynamic creation form. Twin of
    `step_requirement` but attached to the TEMPLATE (not a step) and
    SEPARATE in purpose: collected once at creation, vs requirements that
    ask the client in-flight. The same field may appear in both, freely.

    `kind` ∈ base_field | custom_field (no `document` — documents are
    requirements, not creation fields). `reference`: base → a collectable
    case_person field; custom → a custom_field_definition key (resolved /
    flagged is_archived at read time, never copied). `required_at_creation`
    drives form validation in the case-creation wave."""

    __tablename__ = "journey_template_field"
    __table_args__ = (
        UniqueConstraint("template_id", "kind", "reference", name="uq_journey_template_field"),
    )

    template_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("journey_template.id", ondelete="CASCADE"), index=True, nullable=False
    )
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    reference: Mapped[str] = mapped_column(String(100), nullable=False)
    position: Mapped[int] = mapped_column(default=0, nullable=False)
    required_at_creation: Mapped[bool] = mapped_column(
        default=False, server_default=text("false"), nullable=False
    )
    # Sections chantier (vague A): NULL = the default "unsectioned"
    # bucket. SET NULL on section delete (the field declaration survives).
    section_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("journey_section.id", ondelete="SET NULL"), index=True
    )


class JourneyTemplateCaseField(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A CASE-LEVEL field a template collects at case creation (option b)
    — origin/destination country. SEPARATE from `journey_template_field`
    on purpose: those reference person fields (case_person), these
    reference columns on `client_case`. Keeping them in distinct tables
    preserves the invariant that `journey_template_field.reference` is
    always a person field — no `target` discriminator leaking into the
    person-centric machinery (requirements_eval, materialization).

    `case_field` ∈ COLLECTABLE_CASE_FIELDS (validated in the manager). The
    value is NEVER stored here: it is written to `client_case` via the
    existing top-level create keys; this row only DECLARES that the
    creation form collects it (+ the required gate, + display order)."""

    __tablename__ = "journey_template_case_field"
    __table_args__ = (
        UniqueConstraint("template_id", "case_field", name="uq_journey_template_case_field"),
    )

    template_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("journey_template.id", ondelete="CASCADE"), index=True, nullable=False
    )
    case_field: Mapped[str] = mapped_column(String(30), nullable=False)
    position: Mapped[int] = mapped_column(default=0, nullable=False)
    required_at_creation: Mapped[bool] = mapped_column(
        default=False, server_default=text("false"), nullable=False
    )
    # Sections chantier (vague A): NULL = the default "unsectioned"
    # bucket. SET NULL on section delete (the field declaration survives).
    section_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("journey_section.id", ondelete="SET NULL"), index=True
    )

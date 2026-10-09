"""The Email module: campaigns, their steps and the client profiles they reach.

Ported from Prism (`shared/models/email_campaign.py`, reference commit 629a2b0).
Nidria is the source of truth; the provider (Lemlist) only executes. A
campaign belongs to one agency. Nidria renders every email itself and freezes
the result in `EmailMemberRender`; the provider receives that exact content,
and once an email is sent its render can no longer change (trigger below).

Adaptations to Nidria: UUID primary keys, singular table names, `agency_id`
instead of a project, the recipient is a `client_profile` (never an expat
account, a case person or an external contact), agent foreign keys
(`*_agent_id`), and the agent who paused or removed a membership is kept on
the row because the module writes nothing to `activity_log`.

Every column with a default carries it on BOTH sides (Python `default` and
`server_default`): the test database is born from these models, dev and
production from the migration, and the two must agree (lesson D15).

`email_suppression` is the agency's opposition list (decision of
08/10/2026): a fingerprint of every address that unsubscribed or bounced,
kept whatever happens to the campaign or the client record it came from.

A campaign has an `origin` (decision of 08/10/2026, lot C): NIDRIA, made
in Nidria as above, or LEMLIST_IMPORT, a campaign that already ran in the
agency's Lemlist team and is mirrored read only. An imported campaign's
content belongs to Lemlist: its steps keep the raw Lemlist step in
`provider_content`, and no render exists for its emails.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    DDL,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    event,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from shared.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

#: The only provider today.
PROVIDER_LEMLIST = "LEMLIST"

#: Where a campaign comes from. NIDRIA: made in Nidria, which renders every
#: email and pushes it. LEMLIST_IMPORT: a campaign that already ran in the
#: agency's Lemlist team, mirrored read only; its content is Lemlist's and
#: Nidria never writes it there (only a pause, a resume or an archive of
#: the campaign, and a lead's pause, resume or removal).
ORIGIN_NIDRIA = "NIDRIA"
ORIGIN_LEMLIST_IMPORT = "LEMLIST_IMPORT"
CAMPAIGN_ORIGINS = (ORIGIN_NIDRIA, ORIGIN_LEMLIST_IMPORT)
#: A step's channel: an email, or another channel an imported campaign runs
#: at Lemlist (LinkedIn, a call, a task...), whose exact Lemlist type stays
#: in `provider_content`.
STEP_TYPE_EMAIL = "EMAIL"
STEP_TYPE_OTHER = "OTHER_CHANNEL"

#: Campaign lifecycle.
CAMPAIGN_STATUSES = ("DRAFT", "ACTIVE", "PAUSED", "ARCHIVED")
#: One client profile in one campaign. READY = in Nidria only; QUEUED = handed
#: to the provider, nothing sent yet; ACTIVE = at least one email sent.
MEMBER_STATUSES = (
    "EMAIL_READY",
    "EMAIL_QUEUED",
    "EMAIL_ACTIVE",
    "EMAIL_PAUSED",
    "EMAIL_REPLIED",
    "EMAIL_BOUNCED",
    "EMAIL_COMPLETED",
    "EMAIL_REMOVED",
    "EMAIL_UNSUBSCRIBED",
)
#: A client profile holds at most one live membership across every campaign.
LIVE_MEMBER_STATUSES = ("EMAIL_READY", "EMAIL_QUEUED", "EMAIL_ACTIVE", "EMAIL_PAUSED")
#: A membership that will never send again.
TERMINAL_MEMBER_STATUSES = (
    "EMAIL_REPLIED",
    "EMAIL_BOUNCED",
    "EMAIL_UNSUBSCRIBED",
    "EMAIL_COMPLETED",
    "EMAIL_REMOVED",
)
#: Block the person for good within the agency (N6): Nidria writes to people.
BLOCKING_MEMBER_STATUSES = ("EMAIL_UNSUBSCRIBED", "EMAIL_BOUNCED")
#: Why an address is on the opposition list. An unsubscribe outranks a bounce.
SUPPRESSION_REASONS = ("UNSUBSCRIBED", "BOUNCED")
EVENT_TYPES = (
    "SENT",
    "OPENED",
    "CLICKED",
    "REPLIED",
    "BOUNCED",
    "FAILED",
    "UNSUBSCRIBED",
    "PAUSED",
    "RESUMED",
    "COMPLETED",
    # One variable of a contact unsubscribed at the provider (Lemlist's
    # `variableUnsubscribed`): stored and counted, never applied, since the
    # provider does not document which variable it names.
    "VARIABLE_UNSUBSCRIBED",
)
DELAY_UNITS = ("DAYS", "WEEKS")
MESSAGE_DIRECTIONS = ("OUTBOUND", "INBOUND")
#: An outbound email's furthest state, or REPLIED for an inbound reply, in
#: RANK order (the index is the rank). QUEUED is never stored: a pushed step
#: not sent yet is shown QUEUED from its render.
MESSAGE_STATUSES = ("QUEUED", "SENT", "DELIVERED", "BOUNCED", "OPENED", "CLICKED", "REPLIED")
#: Where a message's content is. RENDERED: an outbound email, whose exact
#: content is its frozen render. PENDING / AVAILABLE / UNAVAILABLE: a reply's
#: content, read from the provider's inbox when it provides it.
#: PROVIDER_ONLY: an outbound email of an imported campaign, sent by Lemlist
#: from its own template, whose personalized text Nidria has not read; the
#: job reads it from the contact's inbox, and the email is then AVAILABLE.
CONTENT_STATES = ("RENDERED", "PENDING", "AVAILABLE", "UNAVAILABLE", "PROVIDER_ONLY")

_EMPTY_JSON = text("'{}'::jsonb")
_LIVE_MEMBER_PREDICATE = "status IN ({})".format(
    ", ".join(f"'{status}'" for status in LIVE_MEMBER_STATUSES)
)
#: A campaign Lemlist knows: a provider id that is not a dry run's (the
#: `dry_` prefix of `providers.dry_run.DRY_RUN_PREFIX`, spelled here: a
#: model never imports application code).
_REAL_PROVIDER_CAMPAIGN = (
    "provider_campaign_id IS NOT NULL AND provider_campaign_id <> '' "
    "AND left(provider_campaign_id, 4) <> 'dry_'"
)
_ORIGIN_CHECK = "origin IN ({})".format(", ".join(f"'{origin}'" for origin in CAMPAIGN_ORIGINS))


class EmailCampaign(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "email_campaign"
    __table_args__ = (
        Index("ix_email_campaign_provider_campaign", "provider", "provider_campaign_id"),
        # One campaign per real provider campaign: what keeps an import
        # idempotent, two concurrent ones included. A dry run's ids are
        # left out (they exist in Nidria only).
        Index(
            "uq_email_campaign_provider_campaign_real",
            "provider",
            "provider_campaign_id",
            unique=True,
            postgresql_where=text(_REAL_PROVIDER_CAMPAIGN),
        ),
        CheckConstraint(_ORIGIN_CHECK, name="origin"),
    )

    agency_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agency.id", ondelete="CASCADE"), index=True, nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    provider: Mapped[str] = mapped_column(
        String(30), nullable=False, default=PROVIDER_LEMLIST, server_default=PROVIDER_LEMLIST
    )
    # NIDRIA, or LEMLIST_IMPORT: a mirror of a Lemlist campaign (read only).
    origin: Mapped[str] = mapped_column(
        String(20), nullable=False, default=ORIGIN_NIDRIA, server_default=ORIGIN_NIDRIA
    )
    provider_campaign_id: Mapped[str | None] = mapped_column(String(100))
    # Lemlist builds one sequence and one schedule per campaign; the steps hang
    # off the sequence, the sending days and hours live in the schedule.
    provider_sequence_id: Mapped[str | None] = mapped_column(String(100))
    provider_schedule_id: Mapped[str | None] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="DRAFT", server_default="DRAFT"
    )
    settings: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=_EMPTY_JSON
    )
    # The manual sync (push to the provider): when, and why it stopped if it did.
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sync_error: Mapped[str | None] = mapped_column(Text)
    # The reconciliation (sync job): the provider's own view, read only.
    # Nidria's name, status and settings are never overwritten from it.
    provider_status: Mapped[str | None] = mapped_column(String(30))
    # Carries `team_id`: the Lemlist team the campaign was created in (N4).
    provider_metadata: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=_EMPTY_JSON
    )
    last_provider_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_provider_error: Mapped[str | None] = mapped_column(Text)
    # Activities are read again from this point, minus an overlap.
    last_activity_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by_agent_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("agent.id", ondelete="SET NULL")
    )

    steps: Mapped[list[EmailCampaignStep]] = relationship(
        back_populates="campaign",
        cascade="all, delete-orphan",
        order_by="EmailCampaignStep.position",
    )


class EmailCampaignStep(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One email of a campaign's sequence. `created_at` matters: it tells a
    step added after a launch from the original sequence."""

    __tablename__ = "email_campaign_step"

    campaign_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("email_campaign.id", ondelete="CASCADE"), index=True, nullable=False
    )
    # 1-based, contiguous within a campaign; the manager keeps it so.
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    type: Mapped[str] = mapped_column(
        String(20), nullable=False, default="EMAIL", server_default="EMAIL"
    )
    delay_value: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    delay_unit: Mapped[str] = mapped_column(
        String(10), nullable=False, default="DAYS", server_default="DAYS"
    )
    subject_template: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default=""
    )
    body_template: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    provider_step_id: Mapped[str | None] = mapped_column(String(100))
    # Which template produced an email: `version` goes up whenever the content
    # (subject, body, AI prompts) changes; `content_hash` identifies it.
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=text("1")
    )
    content_hash: Mapped[str] = mapped_column(
        String(64), nullable=False, default="", server_default=""
    )
    # The content the provider last received; differing from `content_hash`
    # means unsynced changes.
    provider_synced_hash: Mapped[str | None] = mapped_column(String(64))
    # An imported campaign's step as Lemlist returned it (its type, subject,
    # message, delay, index, A/B flag...): the content Lemlist sends. NULL
    # for a step made in Nidria, whose templates are the content.
    provider_content: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    campaign: Mapped[EmailCampaign] = relationship(back_populates="steps")
    ai_variables: Mapped[list[EmailCampaignAiVariable]] = relationship(
        back_populates="step",
        cascade="all, delete-orphan",
        order_by="EmailCampaignAiVariable.key",
    )


class EmailCampaignAiVariable(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "email_campaign_ai_variable"
    __table_args__ = (UniqueConstraint("campaign_step_id", "key", name="uq_email_ai_variable_key"),)

    # Named: the conventional name would exceed PostgreSQL's 63 characters.
    campaign_step_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(
            "email_campaign_step.id",
            ondelete="CASCADE",
            name="fk_email_campaign_ai_variable_step",
        ),
        index=True,
        nullable=False,
    )
    key: Mapped[str] = mapped_column(String(64), nullable=False)
    prompt_template: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str | None] = mapped_column(String(100))
    config: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=_EMPTY_JSON
    )

    step: Mapped[EmailCampaignStep] = relationship(back_populates="ai_variables")


class EmailCampaignMember(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One client profile's membership of one campaign. Never deleted by the
    module: a removal or a move ends it (`removed_at`) and a move points at
    the membership it became."""

    __tablename__ = "email_campaign_member"
    __table_args__ = (
        # Never twice in the same campaign while the membership is open...
        Index(
            "uq_email_campaign_member_open",
            "campaign_id",
            "client_profile_id",
            unique=True,
            postgresql_where=text("removed_at IS NULL"),
        ),
        # ...and never in two campaigns at once while it is live. A client
        # profile belongs to one agency, so this holds per agency.
        Index(
            "uq_email_member_live",
            "client_profile_id",
            unique=True,
            postgresql_where=text(_LIVE_MEMBER_PREDICATE),
        ),
        # Every provider event is matched to its membership by lead id.
        Index("ix_email_campaign_member_provider_lead", "campaign_id", "provider_lead_id"),
    )

    campaign_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("email_campaign.id", ondelete="CASCADE"), index=True, nullable=False
    )
    client_profile_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("client_profile.id", ondelete="CASCADE"), index=True, nullable=False
    )
    # The campaign's agency, kept on the row so every query stays agency-scoped.
    agency_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agency.id", ondelete="CASCADE"), index=True, nullable=False
    )
    provider_lead_id: Mapped[str | None] = mapped_column(String(100))
    provider_contact_id: Mapped[str | None] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(
        String(30), nullable=False, default="EMAIL_READY", server_default="EMAIL_READY", index=True
    )
    # Why it is in that state when it matters: a stable code, never a sentence.
    status_reason: Mapped[str | None] = mapped_column(Text)
    current_step: Mapped[int | None] = mapped_column(Integer)
    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    # Handed to the provider (EMAIL_QUEUED); the campaign's daily limit counts these.
    queued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    paused_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_event_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_event_type: Mapped[str | None] = mapped_column(String(30))
    last_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_clicked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_replied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # The provider's own word on this lead (its state, as it said it), kept
    # verbatim next to the status Nidria derives.
    provider_status: Mapped[str | None] = mapped_column(String(60))
    provider_metadata: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=_EMPTY_JSON
    )
    moved_to_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("email_campaign_member.id", ondelete="SET NULL")
    )
    added_by_agent_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("agent.id", ondelete="SET NULL")
    )
    # Set by a manual pause; NULL for a system pause (an email that could not
    # be rendered). The module writes no activity_log row (N7): these are the
    # trace of an agent's gesture.
    paused_by_agent_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("agent.id", ondelete="SET NULL")
    )
    # Set by a removal or a move to another campaign.
    removed_by_agent_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("agent.id", ondelete="SET NULL")
    )

    campaign: Mapped[EmailCampaign] = relationship()


class EmailMemberRender(UUIDPrimaryKeyMixin, Base):
    """The exact email one membership gets at one step. Created or refreshed
    before the provider receives it; frozen for good once sent (`sent_at`)."""

    __tablename__ = "email_member_render"
    __table_args__ = (
        UniqueConstraint(
            "campaign_member_id", "step_position", name="uq_email_render_member_position"
        ),
    )

    campaign_member_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("email_campaign_member.id", ondelete="CASCADE"), index=True, nullable=False
    )
    # Kept when the step is deleted later: the email that went out stays auditable.
    campaign_step_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("email_campaign_step.id", ondelete="SET NULL"), index=True
    )
    step_position: Mapped[int] = mapped_column(Integer, nullable=False)
    subject: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    resolved_variables: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=_EMPTY_JSON
    )
    # {key: {value, prompt_hash}}
    ai_values: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=_EMPTY_JSON
    )
    template_version: Mapped[int] = mapped_column(Integer, nullable=False)
    template_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    rendered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    provider_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class EmailProviderEvent(UUIDPrimaryKeyMixin, Base):
    """What the provider reported, raw and normalized. `provider_event_id` makes
    every ingestion idempotent: an event delivered by the webhook, the job and a
    manual sync is stored once. Only an event whose agency is known is
    stored, hence a NOT NULL agency: its campaign's, or for an event about a
    whole contact without a campaign Nidria runs (Lemlist's
    `entityUnsubscribed`, `variableUnsubscribed`), the agency bound to its
    Lemlist team; `campaign_id` is then the campaign of the membership it is
    filed under, if any."""

    __tablename__ = "email_provider_event"
    __table_args__ = (
        UniqueConstraint("provider", "provider_event_id", name="uq_email_provider_event"),
        # A membership's history and a campaign's latest event, both by time.
        Index("ix_email_provider_event_member_time", "campaign_member_id", "occurred_at"),
        Index("ix_email_provider_event_campaign_time", "campaign_id", "occurred_at"),
    )

    provider: Mapped[str] = mapped_column(String(30), nullable=False)
    provider_event_id: Mapped[str] = mapped_column(String(100), nullable=False)
    agency_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agency.id", ondelete="CASCADE"), index=True, nullable=False
    )
    campaign_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("email_campaign.id", ondelete="SET NULL"), index=True
    )
    # Named: the conventional name would exceed PostgreSQL's 63 characters.
    campaign_member_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey(
            "email_campaign_member.id",
            ondelete="SET NULL",
            name="fk_email_provider_event_member",
        ),
        index=True,
    )
    type: Mapped[str] = mapped_column(String(30), nullable=False)
    provider_type: Mapped[str] = mapped_column(String(60), nullable=False)
    step_position: Mapped[int | None] = mapped_column(Integer)
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=_EMPTY_JSON
    )
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class EmailMessage(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One email of a membership's conversation, as the provider reported it.

    OUTBOUND: an email the provider sent, one per step, created at its first
    event; its exact content is the frozen render (`render_id`), never copied.
    Opens and clicks are aggregated here (count, first, last); the raw events
    stay in `email_provider_event`. INBOUND: a reply, with its content when
    the provider's inbox returns it. Its `body_html` is untrusted and never
    rendered as HTML. `provider_message_id` is the provider's activity id, so
    one provider event never makes two messages.
    """

    __tablename__ = "email_message"
    __table_args__ = (
        Index(
            "uq_email_message_provider_id",
            "provider",
            "provider_message_id",
            unique=True,
            postgresql_where=text("provider_message_id IS NOT NULL"),
        ),
        Index(
            "uq_email_message_outbound_step",
            "campaign_member_id",
            "step_position",
            unique=True,
            postgresql_where=text("direction = 'OUTBOUND' AND step_position IS NOT NULL"),
        ),
        Index("ix_email_message_member_step", "campaign_member_id", "step_position"),
        Index(
            "ix_email_message_pending_content",
            "campaign_id",
            postgresql_where=text("content_state = 'PENDING'"),
        ),
    )

    agency_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agency.id", ondelete="CASCADE"), index=True, nullable=False
    )
    campaign_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("email_campaign.id", ondelete="CASCADE"), index=True, nullable=False
    )
    campaign_member_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("email_campaign_member.id", ondelete="CASCADE"), nullable=False
    )
    campaign_step_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("email_campaign_step.id", ondelete="SET NULL")
    )
    render_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("email_member_render.id", ondelete="SET NULL")
    )
    step_position: Mapped[int | None] = mapped_column(Integer)
    provider: Mapped[str] = mapped_column(String(30), nullable=False)
    provider_message_id: Mapped[str | None] = mapped_column(String(100))
    # Lemlist keeps one inbox conversation per contact: its contact id.
    provider_thread_id: Mapped[str | None] = mapped_column(String(100))
    direction: Mapped[str] = mapped_column(String(10), nullable=False)
    type: Mapped[str] = mapped_column(
        String(20), nullable=False, default="EMAIL", server_default="EMAIL"
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    subject: Mapped[str | None] = mapped_column(Text)
    body_text: Mapped[str | None] = mapped_column(Text)
    body_html: Mapped[str | None] = mapped_column(Text)
    content_state: Mapped[str] = mapped_column(String(20), nullable=False)
    content_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    first_opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_clicked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    replied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    bounced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    open_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    click_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=_EMPTY_JSON
    )


class EmailSuppression(UUIDPrimaryKeyMixin, Base):
    """The agency's opposition list (decision of 08/10/2026): one row per
    address that unsubscribed or bounced, written each time such a status
    is applied to a membership, read wherever the durable block is
    (`readiness`). It keeps only a fingerprint of the address
    (`email_campaigns.suppression.email_hash`), so it outlives the campaign
    and the client record it came from: deleting either never lifts the
    block. It goes with its agency (CASCADE). An unsubscribe outranks a
    bounce, never the reverse; `source_member_id` is the membership that
    brought the reason kept, NULL once that membership is gone."""

    __tablename__ = "email_suppression"
    __table_args__ = (
        UniqueConstraint("agency_id", "email_hash", name="uq_email_suppression_address"),
        CheckConstraint("reason IN ('UNSUBSCRIBED', 'BOUNCED')", name="reason"),
    )

    agency_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agency.id", ondelete="CASCADE"), nullable=False
    )
    #: sha256, in hexadecimal, of the address lower-cased without any blank.
    email_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    reason: Mapped[str] = mapped_column(String(20), nullable=False)
    source_member_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("email_campaign_member.id", ondelete="SET NULL"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


#: A sent email's render never changes again, whatever writes to it. The
#: manager already refuses; the database makes it impossible. The one write
#: let through is the foreign key's own: deleting a step sets
#: `campaign_step_id` to NULL on its renders, sent ones included.
#: Declared twice, like Prism: here (the test database is born from the
#: models through `create_all`) and in the migration (dev and production).
RENDER_FREEZE_FUNCTION_SQL = """
    CREATE OR REPLACE FUNCTION email_render_freeze() RETURNS trigger AS $$
    BEGIN
      IF OLD.sent_at IS NOT NULL AND (
           NEW.campaign_member_id IS DISTINCT FROM OLD.campaign_member_id
        OR (NEW.campaign_step_id IS DISTINCT FROM OLD.campaign_step_id
            AND NEW.campaign_step_id IS NOT NULL)
        OR NEW.step_position IS DISTINCT FROM OLD.step_position
        OR NEW.subject IS DISTINCT FROM OLD.subject
        OR NEW.body IS DISTINCT FROM OLD.body
        OR NEW.resolved_variables IS DISTINCT FROM OLD.resolved_variables
        OR NEW.ai_values IS DISTINCT FROM OLD.ai_values
        OR NEW.template_version IS DISTINCT FROM OLD.template_version
        OR NEW.template_hash IS DISTINCT FROM OLD.template_hash
        OR NEW.rendered_at IS DISTINCT FROM OLD.rendered_at
        OR NEW.provider_synced_at IS DISTINCT FROM OLD.provider_synced_at
        OR NEW.sent_at IS DISTINCT FROM OLD.sent_at
      ) THEN
        RAISE EXCEPTION 'EMAIL_RENDER_SENT_IS_IMMUTABLE' USING ERRCODE = '42501';
      END IF;
      RETURN NEW;
    END;
    $$ LANGUAGE plpgsql
"""
RENDER_FREEZE_TRIGGER_SQL = """
    CREATE TRIGGER email_render_freeze
    BEFORE UPDATE ON email_member_render
    FOR EACH ROW EXECUTE FUNCTION email_render_freeze()
"""
event.listen(
    EmailMemberRender.__table__,
    "after_create",
    DDL(RENDER_FREEZE_FUNCTION_SQL).execute_if(dialect="postgresql"),  # type: ignore[no-untyped-call]
)
event.listen(
    EmailMemberRender.__table__,
    "after_create",
    DDL(RENDER_FREEZE_TRIGGER_SQL).execute_if(dialect="postgresql"),  # type: ignore[no-untyped-call]
)

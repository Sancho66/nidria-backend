"""Email module: per-agency activation, campaigns, steps, AI variables,
memberships, renders, provider events, messages and the opposition list.

Ported from Prism, where it took two revisions (d1e7a5c3b9f2 + f4c2a8d6b1e3);
Nidria merges them into this one. Additive only: nine new tables, their
indexes, deny-all row-level security on each (no policy, the Nidria rule),
and the trigger that freezes a render once its email is sent. No existing
table is touched, nothing to backfill: the tables are born empty and the
module is closed by default (EMAIL_MODULE_ENABLED=false, no agency row).

`email_suppression` (the agency's opposition list, decision of 08/10/2026)
was added to this revision in place: it had only run on throwaway
databases. So were, for the import of the Lemlist campaigns already
running (lot C, same day), `email_campaign.origin` (NIDRIA or
LEMLIST_IMPORT, checked), `email_campaign_step.provider_content` (the raw
Lemlist step of an imported campaign) and the unique index of one
campaign per real provider campaign id.

Every constraint and index carries an explicit name, identical to what the
models produce through the naming convention, so a schema born from this
migration and one born from `create_all` compare equal
(tests/test_email_campaigns_migration.py).

Revision ID: h9e5b1f7c3d0
Revises: f7c3d9e5a1b2
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "h9e5b1f7c3d0"
down_revision = "f7c3d9e5a1b2"
branch_labels = None
depends_on = None

_UUID = postgresql.UUID(as_uuid=True)
_EMPTY_JSON = sa.text("'{}'::jsonb")
_NOW = sa.text("now()")


def _jsonb(name: str) -> sa.Column:
    return sa.Column(
        name, postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=_EMPTY_JSON
    )


def _ts(name: str) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=True)


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=_NOW),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=_NOW),
    ]


# Frozen copy of shared/models/email_campaign.py RENDER_FREEZE_*: a migration
# never imports application code, which will move.
_RENDER_FREEZE_FUNCTION = """
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
_RENDER_FREEZE_TRIGGER = """
    CREATE TRIGGER email_render_freeze
    BEFORE UPDATE ON email_member_render
    FOR EACH ROW EXECUTE FUNCTION email_render_freeze()
"""
# Frozen copies of shared/models/email_campaign.py `_REAL_PROVIDER_CAMPAIGN`
# and `_ORIGIN_CHECK`.
_REAL_PROVIDER_CAMPAIGN = (
    "provider_campaign_id IS NOT NULL AND provider_campaign_id <> '' "
    "AND left(provider_campaign_id, 4) <> 'dry_'"
)
_ORIGIN_CHECK = "origin IN ('NIDRIA', 'LEMLIST_IMPORT')"


def upgrade() -> None:
    # --- agency_email_module: the platform switch and the bound Lemlist team ---
    op.create_table(
        "agency_email_module",
        sa.Column("agency_id", _UUID, nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("provider", sa.String(30), nullable=False, server_default="LEMLIST"),
        sa.Column("provider_team_id", sa.String(64), nullable=True),
        sa.Column("provider_team_name", sa.String(200), nullable=True),
        _ts("enabled_at"),
        _ts("disabled_at"),
        sa.Column("changed_by_agent_id", _UUID, nullable=True),
        *_timestamps(),
        sa.PrimaryKeyConstraint("agency_id", name="pk_agency_email_module"),
        sa.ForeignKeyConstraint(
            ["agency_id"],
            ["agency.id"],
            name="fk_agency_email_module_agency_id_agency",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["changed_by_agent_id"],
            ["agent.id"],
            name="fk_agency_email_module_changed_by_agent_id_agent",
            ondelete="SET NULL",
        ),
    )
    # One Lemlist team serves one agency (N4).
    op.create_index(
        "uq_agency_email_module_team",
        "agency_email_module",
        ["provider", "provider_team_id"],
        unique=True,
        postgresql_where=sa.text("provider_team_id IS NOT NULL"),
    )
    op.execute("ALTER TABLE agency_email_module ENABLE ROW LEVEL SECURITY")

    # --- email_campaign ---------------------------------------------------------
    op.create_table(
        "email_campaign",
        sa.Column("id", _UUID, nullable=False),
        sa.Column("agency_id", _UUID, nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("provider", sa.String(30), nullable=False, server_default="LEMLIST"),
        sa.Column("origin", sa.String(20), nullable=False, server_default="NIDRIA"),
        sa.Column("provider_campaign_id", sa.String(100), nullable=True),
        sa.Column("provider_sequence_id", sa.String(100), nullable=True),
        sa.Column("provider_schedule_id", sa.String(100), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="DRAFT"),
        _jsonb("settings"),
        _ts("last_synced_at"),
        sa.Column("sync_error", sa.Text(), nullable=True),
        sa.Column("provider_status", sa.String(30), nullable=True),
        _jsonb("provider_metadata"),
        _ts("last_provider_sync_at"),
        sa.Column("last_provider_error", sa.Text(), nullable=True),
        _ts("last_activity_sync_at"),
        sa.Column("created_by_agent_id", _UUID, nullable=True),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name="pk_email_campaign"),
        sa.ForeignKeyConstraint(
            ["agency_id"],
            ["agency.id"],
            name="fk_email_campaign_agency_id_agency",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_agent_id"],
            ["agent.id"],
            name="fk_email_campaign_created_by_agent_id_agent",
            ondelete="SET NULL",
        ),
        sa.CheckConstraint(_ORIGIN_CHECK, name=op.f("ck_email_campaign_origin")),
    )
    op.create_index("ix_email_campaign_agency_id", "email_campaign", ["agency_id"])
    op.create_index(
        "ix_email_campaign_provider_campaign",
        "email_campaign",
        ["provider", "provider_campaign_id"],
    )
    # One campaign per real provider campaign (an import stays idempotent).
    op.create_index(
        "uq_email_campaign_provider_campaign_real",
        "email_campaign",
        ["provider", "provider_campaign_id"],
        unique=True,
        postgresql_where=sa.text(_REAL_PROVIDER_CAMPAIGN),
    )
    op.execute("ALTER TABLE email_campaign ENABLE ROW LEVEL SECURITY")

    # --- email_campaign_step ----------------------------------------------------
    op.create_table(
        "email_campaign_step",
        sa.Column("id", _UUID, nullable=False),
        sa.Column("campaign_id", _UUID, nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("type", sa.String(20), nullable=False, server_default="EMAIL"),
        sa.Column("delay_value", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("delay_unit", sa.String(10), nullable=False, server_default="DAYS"),
        sa.Column("subject_template", sa.Text(), nullable=False, server_default=""),
        sa.Column("body_template", sa.Text(), nullable=False, server_default=""),
        sa.Column("provider_step_id", sa.String(100), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("content_hash", sa.String(64), nullable=False, server_default=""),
        sa.Column("provider_synced_hash", sa.String(64), nullable=True),
        sa.Column(
            "provider_content", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name="pk_email_campaign_step"),
        sa.ForeignKeyConstraint(
            ["campaign_id"],
            ["email_campaign.id"],
            name="fk_email_campaign_step_campaign_id_email_campaign",
            ondelete="CASCADE",
        ),
    )
    op.create_index("ix_email_campaign_step_campaign_id", "email_campaign_step", ["campaign_id"])
    op.execute("ALTER TABLE email_campaign_step ENABLE ROW LEVEL SECURITY")

    # --- email_campaign_ai_variable ---------------------------------------------
    op.create_table(
        "email_campaign_ai_variable",
        sa.Column("id", _UUID, nullable=False),
        sa.Column("campaign_step_id", _UUID, nullable=False),
        sa.Column("key", sa.String(64), nullable=False),
        sa.Column("prompt_template", sa.Text(), nullable=False),
        sa.Column("model", sa.String(100), nullable=True),
        _jsonb("config"),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name="pk_email_campaign_ai_variable"),
        sa.ForeignKeyConstraint(
            ["campaign_step_id"],
            ["email_campaign_step.id"],
            name="fk_email_campaign_ai_variable_step",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("campaign_step_id", "key", name="uq_email_ai_variable_key"),
    )
    op.create_index(
        "ix_email_campaign_ai_variable_campaign_step_id",
        "email_campaign_ai_variable",
        ["campaign_step_id"],
    )
    op.execute("ALTER TABLE email_campaign_ai_variable ENABLE ROW LEVEL SECURITY")

    # --- email_campaign_member --------------------------------------------------
    op.create_table(
        "email_campaign_member",
        sa.Column("id", _UUID, nullable=False),
        sa.Column("campaign_id", _UUID, nullable=False),
        sa.Column("client_profile_id", _UUID, nullable=False),
        sa.Column("agency_id", _UUID, nullable=False),
        sa.Column("provider_lead_id", sa.String(100), nullable=True),
        sa.Column("provider_contact_id", sa.String(100), nullable=True),
        sa.Column("status", sa.String(30), nullable=False, server_default="EMAIL_READY"),
        sa.Column("status_reason", sa.Text(), nullable=True),
        sa.Column("current_step", sa.Integer(), nullable=True),
        sa.Column("added_at", sa.DateTime(timezone=True), nullable=False, server_default=_NOW),
        _ts("queued_at"),
        _ts("started_at"),
        _ts("paused_at"),
        _ts("removed_at"),
        _ts("last_event_at"),
        sa.Column("last_event_type", sa.String(30), nullable=True),
        _ts("last_sent_at"),
        _ts("last_opened_at"),
        _ts("last_clicked_at"),
        _ts("last_replied_at"),
        sa.Column("provider_status", sa.String(60), nullable=True),
        _jsonb("provider_metadata"),
        sa.Column("moved_to_id", _UUID, nullable=True),
        sa.Column("added_by_agent_id", _UUID, nullable=True),
        sa.Column("paused_by_agent_id", _UUID, nullable=True),
        sa.Column("removed_by_agent_id", _UUID, nullable=True),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name="pk_email_campaign_member"),
        sa.ForeignKeyConstraint(
            ["campaign_id"],
            ["email_campaign.id"],
            name="fk_email_campaign_member_campaign_id_email_campaign",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["client_profile_id"],
            ["client_profile.id"],
            name="fk_email_campaign_member_client_profile_id_client_profile",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["agency_id"],
            ["agency.id"],
            name="fk_email_campaign_member_agency_id_agency",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["moved_to_id"],
            ["email_campaign_member.id"],
            name="fk_email_campaign_member_moved_to_id_email_campaign_member",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["added_by_agent_id"],
            ["agent.id"],
            name="fk_email_campaign_member_added_by_agent_id_agent",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["paused_by_agent_id"],
            ["agent.id"],
            name="fk_email_campaign_member_paused_by_agent_id_agent",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["removed_by_agent_id"],
            ["agent.id"],
            name="fk_email_campaign_member_removed_by_agent_id_agent",
            ondelete="SET NULL",
        ),
    )
    for column in ("agency_id", "campaign_id", "client_profile_id", "status"):
        op.create_index(f"ix_email_campaign_member_{column}", "email_campaign_member", [column])
    op.create_index(
        "ix_email_campaign_member_provider_lead",
        "email_campaign_member",
        ["campaign_id", "provider_lead_id"],
    )
    # Never twice in the same campaign while the membership is open...
    op.create_index(
        "uq_email_campaign_member_open",
        "email_campaign_member",
        ["campaign_id", "client_profile_id"],
        unique=True,
        postgresql_where=sa.text("removed_at IS NULL"),
    )
    # ...and never in two campaigns at once while it is live.
    op.create_index(
        "uq_email_member_live",
        "email_campaign_member",
        ["client_profile_id"],
        unique=True,
        postgresql_where=sa.text(
            "status IN ('EMAIL_READY', 'EMAIL_QUEUED', 'EMAIL_ACTIVE', 'EMAIL_PAUSED')"
        ),
    )
    op.execute("ALTER TABLE email_campaign_member ENABLE ROW LEVEL SECURITY")

    # --- email_member_render (no timestamps: rendered_at / sent_at say it) -------
    op.create_table(
        "email_member_render",
        sa.Column("id", _UUID, nullable=False),
        sa.Column("campaign_member_id", _UUID, nullable=False),
        sa.Column("campaign_step_id", _UUID, nullable=True),
        sa.Column("step_position", sa.Integer(), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        _jsonb("resolved_variables"),
        _jsonb("ai_values"),
        sa.Column("template_version", sa.Integer(), nullable=False),
        sa.Column("template_hash", sa.String(64), nullable=False),
        sa.Column("rendered_at", sa.DateTime(timezone=True), nullable=False, server_default=_NOW),
        _ts("provider_synced_at"),
        _ts("sent_at"),
        sa.PrimaryKeyConstraint("id", name="pk_email_member_render"),
        sa.ForeignKeyConstraint(
            ["campaign_member_id"],
            ["email_campaign_member.id"],
            name="fk_email_member_render_campaign_member_id_email_campaign_member",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["campaign_step_id"],
            ["email_campaign_step.id"],
            name="fk_email_member_render_campaign_step_id_email_campaign_step",
            ondelete="SET NULL",
        ),
        sa.UniqueConstraint(
            "campaign_member_id", "step_position", name="uq_email_render_member_position"
        ),
    )
    for column in ("campaign_member_id", "campaign_step_id"):
        op.create_index(f"ix_email_member_render_{column}", "email_member_render", [column])
    # A sent render is frozen. The only write let through is the foreign key's
    # own: deleting a step sets campaign_step_id to NULL on its renders.
    op.execute(_RENDER_FREEZE_FUNCTION)
    op.execute(_RENDER_FREEZE_TRIGGER)
    op.execute("ALTER TABLE email_member_render ENABLE ROW LEVEL SECURITY")

    # --- email_provider_event (no updated_at: an event is written once) ---------
    op.create_table(
        "email_provider_event",
        sa.Column("id", _UUID, nullable=False),
        sa.Column("provider", sa.String(30), nullable=False),
        sa.Column("provider_event_id", sa.String(100), nullable=False),
        sa.Column("agency_id", _UUID, nullable=False),
        sa.Column("campaign_id", _UUID, nullable=True),
        sa.Column("campaign_member_id", _UUID, nullable=True),
        sa.Column("type", sa.String(30), nullable=False),
        sa.Column("provider_type", sa.String(60), nullable=False),
        sa.Column("step_position", sa.Integer(), nullable=True),
        _jsonb("payload"),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=_NOW),
        sa.PrimaryKeyConstraint("id", name="pk_email_provider_event"),
        sa.ForeignKeyConstraint(
            ["agency_id"],
            ["agency.id"],
            name="fk_email_provider_event_agency_id_agency",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["campaign_id"],
            ["email_campaign.id"],
            name="fk_email_provider_event_campaign_id_email_campaign",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["campaign_member_id"],
            ["email_campaign_member.id"],
            name="fk_email_provider_event_member",
            ondelete="SET NULL",
        ),
        # The heart of idempotence: webhook, job and manual sync store an event once.
        sa.UniqueConstraint("provider", "provider_event_id", name="uq_email_provider_event"),
    )
    for column in ("agency_id", "campaign_id", "campaign_member_id"):
        op.create_index(f"ix_email_provider_event_{column}", "email_provider_event", [column])
    op.create_index(
        "ix_email_provider_event_member_time",
        "email_provider_event",
        ["campaign_member_id", "occurred_at"],
    )
    op.create_index(
        "ix_email_provider_event_campaign_time",
        "email_provider_event",
        ["campaign_id", "occurred_at"],
    )
    op.execute("ALTER TABLE email_provider_event ENABLE ROW LEVEL SECURITY")

    # --- email_message ----------------------------------------------------------
    op.create_table(
        "email_message",
        sa.Column("id", _UUID, nullable=False),
        sa.Column("agency_id", _UUID, nullable=False),
        sa.Column("campaign_id", _UUID, nullable=False),
        sa.Column("campaign_member_id", _UUID, nullable=False),
        sa.Column("campaign_step_id", _UUID, nullable=True),
        sa.Column("render_id", _UUID, nullable=True),
        sa.Column("step_position", sa.Integer(), nullable=True),
        sa.Column("provider", sa.String(30), nullable=False),
        sa.Column("provider_message_id", sa.String(100), nullable=True),
        sa.Column("provider_thread_id", sa.String(100), nullable=True),
        sa.Column("direction", sa.String(10), nullable=False),
        sa.Column("type", sa.String(20), nullable=False, server_default="EMAIL"),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("subject", sa.Text(), nullable=True),
        sa.Column("body_text", sa.Text(), nullable=True),
        sa.Column("body_html", sa.Text(), nullable=True),
        sa.Column("content_state", sa.String(20), nullable=False),
        sa.Column("content_attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
        _ts("sent_at"),
        _ts("delivered_at"),
        _ts("first_opened_at"),
        _ts("last_opened_at"),
        _ts("last_clicked_at"),
        _ts("replied_at"),
        _ts("bounced_at"),
        _ts("received_at"),
        sa.Column("open_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("click_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        _jsonb("payload"),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name="pk_email_message"),
        sa.ForeignKeyConstraint(
            ["agency_id"],
            ["agency.id"],
            name="fk_email_message_agency_id_agency",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["campaign_id"],
            ["email_campaign.id"],
            name="fk_email_message_campaign_id_email_campaign",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["campaign_member_id"],
            ["email_campaign_member.id"],
            name="fk_email_message_campaign_member_id_email_campaign_member",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["campaign_step_id"],
            ["email_campaign_step.id"],
            name="fk_email_message_campaign_step_id_email_campaign_step",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["render_id"],
            ["email_member_render.id"],
            name="fk_email_message_render_id_email_member_render",
            ondelete="SET NULL",
        ),
    )
    for column in ("agency_id", "campaign_id", "status"):
        op.create_index(f"ix_email_message_{column}", "email_message", [column])
    op.create_index(
        "ix_email_message_member_step", "email_message", ["campaign_member_id", "step_position"]
    )
    # One provider event never makes two messages...
    op.create_index(
        "uq_email_message_provider_id",
        "email_message",
        ["provider", "provider_message_id"],
        unique=True,
        postgresql_where=sa.text("provider_message_id IS NOT NULL"),
    )
    # ...and a step is sent once to a membership.
    op.create_index(
        "uq_email_message_outbound_step",
        "email_message",
        ["campaign_member_id", "step_position"],
        unique=True,
        postgresql_where=sa.text("direction = 'OUTBOUND' AND step_position IS NOT NULL"),
    )
    op.create_index(
        "ix_email_message_pending_content",
        "email_message",
        ["campaign_id"],
        postgresql_where=sa.text("content_state = 'PENDING'"),
    )
    op.execute("ALTER TABLE email_message ENABLE ROW LEVEL SECURITY")

    # --- email_suppression: the agency's opposition list (no updated_at) ----------
    op.create_table(
        "email_suppression",
        sa.Column("id", _UUID, nullable=False),
        sa.Column("agency_id", _UUID, nullable=False),
        sa.Column("email_hash", sa.String(64), nullable=False),
        sa.Column("reason", sa.String(20), nullable=False),
        sa.Column("source_member_id", _UUID, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=_NOW),
        sa.PrimaryKeyConstraint("id", name="pk_email_suppression"),
        sa.ForeignKeyConstraint(
            ["agency_id"],
            ["agency.id"],
            name="fk_email_suppression_agency_id_agency",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_member_id"],
            ["email_campaign_member.id"],
            name="fk_email_suppression_source_member_id_email_campaign_member",
            ondelete="SET NULL",
        ),
        # One row per address and agency: an unsubscribe outranks a bounce.
        sa.UniqueConstraint("agency_id", "email_hash", name="uq_email_suppression_address"),
        sa.CheckConstraint(
            "reason IN ('UNSUBSCRIBED', 'BOUNCED')", name=op.f("ck_email_suppression_reason")
        ),
    )
    op.create_index(
        "ix_email_suppression_source_member_id", "email_suppression", ["source_member_id"]
    )
    op.execute("ALTER TABLE email_suppression ENABLE ROW LEVEL SECURITY")


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS email_render_freeze ON email_member_render")
    op.execute("DROP FUNCTION IF EXISTS email_render_freeze()")
    # Children first; dropping a table drops its indexes and constraints.
    op.drop_table("email_suppression")
    op.drop_table("email_message")
    op.drop_table("email_provider_event")
    op.drop_table("email_member_render")
    op.drop_table("email_campaign_member")
    op.drop_table("email_campaign_ai_variable")
    op.drop_table("email_campaign_step")
    op.drop_table("email_campaign")
    op.drop_table("agency_email_module")

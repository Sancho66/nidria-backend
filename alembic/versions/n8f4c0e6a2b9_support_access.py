"""Agency-authorized support access with content-free audit events.

Revision ID: n8f4c0e6a2b9
Revises: m7e3b9d5f1a8
"""

from alembic import op
import sqlalchemy as sa

revision = "n8f4c0e6a2b9"
down_revision = "m7e3b9d5f1a8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "support_access_grant",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "agency_id", sa.Uuid(), sa.ForeignKey("agency.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "case_id",
            sa.Uuid(),
            sa.ForeignKey("client_case.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "operator_id", sa.Uuid(), sa.ForeignKey("agent.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("granted_by_id", sa.Uuid(), sa.ForeignKey("agent.id", ondelete="SET NULL")),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("revoked_by_id", sa.Uuid(), sa.ForeignKey("agent.id", ondelete="SET NULL")),
        sa.CheckConstraint("expires_at > created_at", name="positive_duration"),
    )
    op.create_table(
        "support_access_event",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "agency_id", sa.Uuid(), sa.ForeignKey("agency.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "case_id",
            sa.Uuid(),
            sa.ForeignKey("client_case.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "grant_id", sa.Uuid(), sa.ForeignKey("support_access_grant.id", ondelete="SET NULL")
        ),
        sa.Column("actor_id", sa.Uuid(), sa.ForeignKey("agent.id", ondelete="SET NULL")),
        sa.Column("action", sa.String(30), nullable=False),
        sa.Column("resource_id", sa.Uuid()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    for table, columns in (
        ("support_access_grant", ("agency_id", "case_id", "operator_id", "expires_at")),
        ("support_access_event", ("agency_id", "case_id", "created_at")),
    ):
        for column in columns:
            op.create_index(f"ix_{table}_{column}", table, [column])
    op.execute("ALTER TABLE support_access_grant ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE support_access_event ENABLE ROW LEVEL SECURITY")


def downgrade() -> None:
    op.drop_table("support_access_event")
    op.drop_table("support_access_grant")

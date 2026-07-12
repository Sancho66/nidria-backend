"""add_paddle_billing

Revision ID: e6f1a8c4d2b7
Revises: d5e0f7b3c9a6
Create Date: 2026-07-12

Paddle (Merchant of Record) plumbing:
- 4 columns on agency: billing_mode (NOT NULL, default 'manual' — the
  non-migration of existing agencies IS this default), billing_status,
  paddle_customer_id, paddle_subscription_id (both UNIQUE);
- paddle_webhook_event: the idempotence gate (event_id UNIQUE) + audit trail.

Additive, reversible: drop the table and the columns.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision: str = "e6f1a8c4d2b7"
down_revision: str | Sequence[str] | None = "d5e0f7b3c9a6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "agency",
        sa.Column(
            "billing_mode",
            sa.String(length=10),
            server_default=sa.text("'manual'"),
            nullable=False,
        ),
    )
    op.add_column("agency", sa.Column("billing_status", sa.String(length=20), nullable=True))
    op.add_column("agency", sa.Column("paddle_customer_id", sa.String(length=64), nullable=True))
    op.add_column(
        "agency", sa.Column("paddle_subscription_id", sa.String(length=64), nullable=True)
    )
    op.create_unique_constraint("uq_agency_paddle_customer_id", "agency", ["paddle_customer_id"])
    op.create_unique_constraint(
        "uq_agency_paddle_subscription_id", "agency", ["paddle_subscription_id"]
    )

    op.create_table(
        "paddle_webhook_event",
        sa.Column("event_id", sa.String(length=64), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("agency_id", sa.Uuid(), nullable=True),
        sa.Column("payload", JSONB(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_paddle_webhook_event_event_id"),
        "paddle_webhook_event",
        ["event_id"],
        unique=True,
    )
    op.create_index(
        op.f("ix_paddle_webhook_event_agency_id"),
        "paddle_webhook_event",
        ["agency_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_paddle_webhook_event_agency_id"), table_name="paddle_webhook_event")
    op.drop_index(op.f("ix_paddle_webhook_event_event_id"), table_name="paddle_webhook_event")
    op.drop_table("paddle_webhook_event")
    op.drop_constraint("uq_agency_paddle_subscription_id", "agency", type_="unique")
    op.drop_constraint("uq_agency_paddle_customer_id", "agency", type_="unique")
    op.drop_column("agency", "paddle_subscription_id")
    op.drop_column("agency", "paddle_customer_id")
    op.drop_column("agency", "billing_status")
    op.drop_column("agency", "billing_mode")

"""Keep every imported campaign participation without enabling double enrollment.

Revision ID: m7e3b9d5f1a8
Revises: l5c9e2a7d4f1
"""

import sqlalchemy as sa

from alembic import op

revision = "m7e3b9d5f1a8"
down_revision = "l5c9e2a7d4f1"
branch_labels = None
depends_on = None

LIVE = "status IN ('EMAIL_READY', 'EMAIL_QUEUED', 'EMAIL_ACTIVE', 'EMAIL_PAUSED')"


def upgrade() -> None:
    op.add_column(
        "email_campaign_member",
        sa.Column(
            "is_provider_mirror", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
    )
    op.execute("""
        UPDATE email_campaign_member m SET is_provider_mirror = true
        FROM email_campaign c
        WHERE m.campaign_id = c.id AND m.agency_id = c.agency_id
          AND c.origin = 'LEMLIST_IMPORT'
    """)
    op.drop_index("uq_email_member_live", table_name="email_campaign_member")
    op.create_index(
        "uq_email_member_live",
        "email_campaign_member",
        ["client_profile_id"],
        unique=True,
        postgresql_where=sa.text(f"{LIVE} AND NOT is_provider_mirror"),
    )


def downgrade() -> None:
    # Refuse a lossy rollback: restoring the old invariant may require a
    # product decision once multiple provider histories have been recovered.
    op.execute(f"""
        DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM email_campaign_member WHERE {LIVE}
                     GROUP BY client_profile_id HAVING count(*) > 1) THEN
            RAISE EXCEPTION 'Multiple provider memberships exist; downgrade would lose history';
          END IF;
        END $$
    """)
    op.drop_index("uq_email_member_live", table_name="email_campaign_member")
    op.create_index(
        "uq_email_member_live",
        "email_campaign_member",
        ["client_profile_id"],
        unique=True,
        postgresql_where=sa.text(LIVE),
    )
    op.drop_column("email_campaign_member", "is_provider_mirror")

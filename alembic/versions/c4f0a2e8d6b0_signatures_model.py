"""E-signatures, socle agnostique (méga-lot 28/07, lot 1)

- step_requirement / case_step_requirement : signature_required (bool,
  défaut false) + signature_level (ses|aes|qes, défaut 'ses') — le pattern
  scope copié (déclaration au template, snapshot sur l'instance).
- signature_request / signature_signer : les deux entités provider-
  agnostiques (refs opaques seulement).

Additif pur, défauts serveur — zéro backfill nécessaire.

Revision ID: c4f0a2e8d6b0
Revises: b2d8f4a6c0e2
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "c4f0a2e8d6b0"
down_revision = "b2d8f4a6c0e2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table in ("step_requirement", "case_step_requirement"):
        op.add_column(
            table,
            sa.Column(
                "signature_required",
                sa.Boolean(),
                server_default=sa.text("false"),
                nullable=False,
            ),
        )
        op.add_column(
            table,
            sa.Column(
                "signature_level",
                sa.String(length=3),
                server_default=sa.text("'ses'"),
                nullable=False,
            ),
        )

    op.create_table(
        "signature_request",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("case_id", sa.Uuid(), nullable=False),
        sa.Column("case_step_progress_id", sa.Uuid(), nullable=False),
        sa.Column("step_requirement_id", sa.Uuid(), nullable=True),
        sa.Column("reference", sa.String(length=100), nullable=False),
        sa.Column("provider", sa.String(length=20), nullable=False),
        sa.Column("level", sa.String(length=3), nullable=False),
        sa.Column(
            "status", sa.String(length=20), server_default=sa.text("'draft'"), nullable=False
        ),
        sa.Column("provider_ref", sa.String(length=255), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.ForeignKeyConstraint(
            ["case_id"],
            ["client_case.id"],
            name=op.f("fk_signature_request_case_id_client_case"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["case_step_progress_id"],
            ["case_step_progress.id"],
            name=op.f("fk_signature_request_case_step_progress_id_case_step_progress"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["step_requirement_id"],
            ["step_requirement.id"],
            name=op.f("fk_signature_request_step_requirement_id_step_requirement"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_signature_request")),
    )
    op.create_index(op.f("ix_signature_request_case_id"), "signature_request", ["case_id"])
    op.create_index(
        op.f("ix_signature_request_case_step_progress_id"),
        "signature_request",
        ["case_step_progress_id"],
    )
    op.create_index(
        op.f("ix_signature_request_step_requirement_id"),
        "signature_request",
        ["step_requirement_id"],
    )
    op.create_index(
        op.f("ix_signature_request_provider_ref"), "signature_request", ["provider_ref"]
    )

    op.create_table(
        "signature_signer",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("signature_request_id", sa.Uuid(), nullable=False),
        sa.Column("case_person_id", sa.Uuid(), nullable=False),
        sa.Column("case_step_requirement_id", sa.Uuid(), nullable=True),
        sa.Column(
            "status", sa.String(length=20), server_default=sa.text("'pending'"), nullable=False
        ),
        sa.Column("signed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("provider_ref", sa.String(length=255), nullable=True),
        sa.Column("provider_slug", sa.String(length=255), nullable=True),
        sa.Column("details", JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
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
        sa.ForeignKeyConstraint(
            ["signature_request_id"],
            ["signature_request.id"],
            name=op.f("fk_signature_signer_signature_request_id_signature_request"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["case_person_id"],
            ["case_person.id"],
            name=op.f("fk_signature_signer_case_person_id_case_person"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["case_step_requirement_id"],
            ["case_step_requirement.id"],
            name=op.f("fk_signature_signer_case_step_requirement_id_case_step_requirement"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_signature_signer")),
        sa.UniqueConstraint("signature_request_id", "case_person_id", name="uq_signature_signer"),
    )
    op.create_index(
        op.f("ix_signature_signer_signature_request_id"),
        "signature_signer",
        ["signature_request_id"],
    )
    op.create_index(
        op.f("ix_signature_signer_case_person_id"), "signature_signer", ["case_person_id"]
    )
    op.create_index(op.f("ix_signature_signer_provider_ref"), "signature_signer", ["provider_ref"])

    # Posture RLS de prod (garde test_rls) : toute table nouvelle est
    # RLS-enabled, deny-all sans policy — PostgREST n'expose jamais nu.
    op.execute('ALTER TABLE "signature_request" ENABLE ROW LEVEL SECURITY')
    op.execute('ALTER TABLE "signature_signer" ENABLE ROW LEVEL SECURITY')


def downgrade() -> None:
    op.drop_table("signature_signer")
    op.drop_table("signature_request")
    for table in ("case_step_requirement", "step_requirement"):
        op.drop_column(table, "signature_level")
        op.drop_column(table, "signature_required")

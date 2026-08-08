"""Lot lecteur (08/08) — le TYPE de siège au modèle + le pool acheté.

Trois colonnes, toutes défautées côté base ET côté modèle (leçon D15 :
les deux vérités disent la même chose dans les deux sens, la garde
test_v0110_schema_defaults_migration y veille) :

- `agent.seat_type` / `agent_invitation.seat_type` ('manager' | 'reader',
  String(10) comme billing_mode — jamais d'enum Postgres) : tout
  l'existant est backfillé 'manager' par le server_default, la vérité
  d'hier (chaque membre était un gestionnaire).
- `agency.reader_seats_purchased` (défaut 0, CHECK >= 0) : le pool de
  sièges lecteur ACHETÉS — la quantité Paddle du SKU lecteur est CE
  pool, jamais le compte de lecteurs actifs. Un geste d'achat = un
  prorata = une ligne de facture (cas Nicolas : +7 lecteurs, un PATCH).

Revision ID: s3e7b1d9c5f2
Revises: r8b4d0f6a2c8
"""

import sqlalchemy as sa
from alembic import op

revision = "s3e7b1d9c5f2"
down_revision = "r8b4d0f6a2c8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "agent",
        sa.Column(
            "seat_type",
            sa.String(length=10),
            nullable=False,
            server_default=sa.text("'manager'"),
        ),
    )
    op.add_column(
        "agent_invitation",
        sa.Column(
            "seat_type",
            sa.String(length=10),
            nullable=False,
            server_default=sa.text("'manager'"),
        ),
    )
    op.add_column(
        "agency",
        sa.Column(
            "reader_seats_purchased",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.create_check_constraint(
        "agency_reader_seats_purchased_check",
        "agency",
        "reader_seats_purchased >= 0",
    )


def downgrade() -> None:
    op.drop_constraint("agency_reader_seats_purchased_check", "agency", type_="check")
    op.drop_column("agency", "reader_seats_purchased")
    op.drop_column("agent_invitation", "seat_type")
    op.drop_column("agent", "seat_type")

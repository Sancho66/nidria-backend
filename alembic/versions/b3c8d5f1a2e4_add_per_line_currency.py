"""add_per_line_currency

Revision ID: b3c8d5f1a2e4
Revises: a2f7c4e9b1d3
Create Date: 2026-07-10

The currency moves ONTO the cost line (a money is paid in a precise currency),
with NO conversion anywhere:
- journey_step_cost.currency (the planned cost's currency, NOT NULL);
- case_step_cost.currency (the REAL amount's currency, NOT NULL) +
  case_step_cost.planned_currency (the frozen planned amount's currency, NULL
  for a manual débours).

Deterministic backfill (point 6, NO assumption): every existing line inherits
its AGENCY's currency (agency.currency), resolved by join. A cost line can only
exist for an agency that had a currency (the old 409 required it), so no line is
left without one — and the NOT NULL added after the backfill proves it. Planned
currencies are backfilled only where a planned amount exists.

Reversible + idempotent (proven on a testcontainer): downgrade drops the three
columns.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b3c8d5f1a2e4"
down_revision: str | Sequence[str] | None = "a2f7c4e9b1d3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1) journey_step_cost.currency — backfill from the template's agency.
    op.add_column("journey_step_cost", sa.Column("currency", sa.String(length=3), nullable=True))
    op.execute(
        """
        UPDATE journey_step_cost AS jsc
        SET currency = a.currency
        FROM journey_template_step AS jts
        JOIN journey_template AS jt ON jts.template_id = jt.id
        JOIN agency AS a ON jt.agency_id = a.id
        WHERE jsc.step_id = jts.id
        """
    )
    op.alter_column("journey_step_cost", "currency", existing_type=sa.String(length=3), nullable=False)

    # 2) case_step_cost.currency — backfill from the case's agency (the REAL
    #    currency of every existing line: they were entered under the old
    #    single-agency-currency rule).
    op.add_column("case_step_cost", sa.Column("currency", sa.String(length=3), nullable=True))
    op.execute(
        """
        UPDATE case_step_cost AS c
        SET currency = a.currency
        FROM case_step_progress AS csp
        JOIN client_case AS cc ON csp.case_id = cc.id
        JOIN agency AS a ON cc.agency_id = a.id
        WHERE c.case_step_progress_id = csp.id
        """
    )
    op.alter_column("case_step_cost", "currency", existing_type=sa.String(length=3), nullable=False)

    # 3) case_step_cost.planned_currency — only where a planned amount exists
    #    (a manual débours keeps it NULL). Same agency currency, frozen.
    op.add_column(
        "case_step_cost", sa.Column("planned_currency", sa.String(length=3), nullable=True)
    )
    op.execute(
        """
        UPDATE case_step_cost AS c
        SET planned_currency = a.currency
        FROM case_step_progress AS csp
        JOIN client_case AS cc ON csp.case_id = cc.id
        JOIN agency AS a ON cc.agency_id = a.id
        WHERE c.case_step_progress_id = csp.id AND c.planned_amount IS NOT NULL
        """
    )


def downgrade() -> None:
    op.drop_column("case_step_cost", "planned_currency")
    op.drop_column("case_step_cost", "currency")
    op.drop_column("journey_step_cost", "currency")

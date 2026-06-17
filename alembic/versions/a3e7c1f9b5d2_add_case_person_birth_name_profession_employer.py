"""add case_person.birth_name / profession / employer (collectable base fields)

Three nullable case_person columns extending the collectable civil/
professional base fields (vague V4). ADDITIVE: all nullable, NO default,
NO backfill — existing rows keep NULL silently. Touches only case_person;
the client_case country/address columns and the query ecosystem are
untouched.

Revision ID: a3e7c1f9b5d2
Revises: f3c7a9e1b5d8
Create Date: 2026-06-17 00:00:00.000000

"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a3e7c1f9b5d2"
down_revision: Union[str, Sequence[str], None] = "f3c7a9e1b5d8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "case_person", sa.Column("birth_name", sa.String(length=200), nullable=True)
    )
    op.add_column(
        "case_person", sa.Column("profession", sa.String(length=200), nullable=True)
    )
    op.add_column(
        "case_person", sa.Column("employer", sa.String(length=200), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("case_person", "employer")
    op.drop_column("case_person", "profession")
    op.drop_column("case_person", "birth_name")

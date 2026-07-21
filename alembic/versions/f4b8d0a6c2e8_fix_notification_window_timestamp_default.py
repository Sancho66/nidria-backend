"""fix missing timestamp server_default on notification_window + digest_cursor

NID-07 hotfix. The d1f7b3e9a5c2 migration created these NOT NULL columns
WITHOUT a server_default, while TimestampMixin declares
server_default=func.now(). The ORM therefore omits created_at from the
INSERT (expecting the DB to fill it) and prod raised NotNullViolation on
every notification_window insert — which the case-creation path does
(record_send at case creation) → POST /cases 503 (and digest_cursor would fail the same way on the Monday digest run). Tests never caught it
because the test schema is built from Base.metadata (create_all applies
the model's server_default); only the migration-built prod schema lacked
it. This migration aligns the DB with the model.

Revision ID: f4b8d0a6c2e8
Revises: e0a6c4b8d2f6
Create Date: 2026-07-21 15:00:00.000000

"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f4b8d0a6c2e8"
down_revision: Union[str, Sequence[str], None] = "e0a6c4b8d2f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_TABLES = ("notification_window", "digest_cursor")


def upgrade() -> None:
    # Both tables' migrations forgot the server_default that TimestampMixin
    # declares (prod scan 2026-07-21: exactly these two). Align the DB.
    for table in _TABLES:
        op.alter_column(table, "created_at", server_default=sa.text("now()"))
        op.alter_column(table, "updated_at", server_default=sa.text("now()"))


def downgrade() -> None:
    for table in _TABLES:
        op.alter_column(table, "created_at", server_default=None)
        op.alter_column(table, "updated_at", server_default=None)

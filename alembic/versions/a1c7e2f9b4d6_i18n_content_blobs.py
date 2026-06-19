"""i18n content blobs — parallel {lang: text} JSONB on the 5 entered-content
fields + agency.default_language (BLOC 1)

Additive and reversible. Strategy = PARALLEL columns: each scalar column stays
populated and remains the READ source; a new `{field}_i18n` JSONB holds the
per-language variants. The projections are NOT switched in this block (BLOC 2),
so current reads are untouched.

Backfill: every row's default-language variant is seeded from the current
(FR) scalar value — `{"fr": <value>}` — since at migration time every agency's
default_language is "fr" and all content is FR. A NULL scalar (content_note,
description) leaves the blob as `{}` (absent language = absent key, never "").

Revision ID: a1c7e2f9b4d6
Revises: f3b8d1e4a9c2
Create Date: 2026-06-19 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a1c7e2f9b4d6"
down_revision: str | Sequence[str] | None = "f3b8d1e4a9c2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_I18N = sa.text("'{}'::jsonb")

# (table, scalar column, i18n column, scalar is nullable)
_FIELDS = [
    ("journey_template_step", "name", "name_i18n", False),
    ("journey_template_step", "content_note", "content_note_i18n", True),
    ("journey_section", "name", "name_i18n", False),
    ("journey_section", "description", "description_i18n", True),
    ("custom_field_definition", "label", "label_i18n", False),
]


def upgrade() -> None:
    # Agency default content language (fallback for its i18n blobs).
    op.add_column(
        "agency",
        sa.Column("default_language", sa.String(length=2), nullable=False, server_default="fr"),
    )
    op.create_check_constraint(
        "agency_default_language_check", "agency", "default_language IN ('fr', 'en', 'es')"
    )

    for table, scalar, i18n, nullable in _FIELDS:
        op.add_column(
            table,
            sa.Column(i18n, postgresql.JSONB(), nullable=False, server_default=_I18N),
        )
        # Seed the FR variant from the current scalar value. NULL scalar → keep
        # the default '{}' (no key) rather than {"fr": null}.
        guard = f" WHERE {scalar} IS NOT NULL" if nullable else ""
        op.execute(
            f"UPDATE {table} SET {i18n} = jsonb_build_object('fr', {scalar}){guard}"  # noqa: S608
        )


def downgrade() -> None:
    for table, _scalar, i18n, _nullable in _FIELDS:
        op.drop_column(table, i18n)
    op.drop_constraint("agency_default_language_check", "agency", type_="check")
    op.drop_column("agency", "default_language")

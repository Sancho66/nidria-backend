"""Lot taxonomie — la section FICHE des définitions de champs.

`custom_field_definition.profile_section` ('identity' | 'contact' |
'id_documents' | 'situation' | 'misc', défaut 'misc') : la taxonomie
PROPRE de la fiche (deux univers assumés — le picker de collecte garde
ses catégories). Backfill des presets du catalogue depuis le mapping
code (src/client_profiles/profile_sections.PRESET_PROFILE_SECTION) ;
les custom d'agence restent 'misc', reclassables par le toggle élargi.
Rejouable (UPDATE par listes de clés).

Revision ID: a4c8e2f6b0d4
Revises: f2d6a0b4c8e2
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from src.client_profiles.profile_sections import PRESET_PROFILE_SECTION

revision: str = "a4c8e2f6b0d4"
down_revision: str | None = "f2d6a0b4c8e2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "custom_field_definition",
        sa.Column(
            "profile_section",
            sa.String(length=20),
            nullable=False,
            server_default="misc",
        ),
    )
    by_section: dict[str, list[str]] = {}
    for key, section in PRESET_PROFILE_SECTION.items():
        by_section.setdefault(section, []).append(key)
    for section, keys in by_section.items():
        keys_sql = ", ".join(f"'{k}'" for k in keys)
        op.execute(
            f"UPDATE custom_field_definition SET profile_section = '{section}' "
            f"WHERE key IN ({keys_sql})"
        )


def downgrade() -> None:
    op.drop_column("custom_field_definition", "profile_section")

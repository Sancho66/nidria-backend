"""Lot dédoublonnage — housing_address et le preset preferred_language
meurent (survivants : residence_address, LA COLONNE preferred_lang).

Toute la logique vit dans src/imports/referential_dedup.dedup_referential
(partagée migration/tests/protocole dump-prod — le précédent backfill).
Idempotente, rejouable ; les comptes sont logués.

Revision ID: g4b8d2f6c0a4
Revises: e2f6b0d4a8c2
"""

from collections.abc import Sequence

from alembic import op

from src.imports.referential_dedup import dedup_referential

revision: str = "g4b8d2f6c0a4"
down_revision: str | None = "e2f6b0d4a8c2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    stats = dedup_referential(op.get_bind())
    print(f"referential dedup: {stats}")


def downgrade() -> None:
    # Migration de données à sens unique (les presets sont retirés du
    # CODE ; recréer les doublons n'aurait pas de sens). No-op assumé.
    pass

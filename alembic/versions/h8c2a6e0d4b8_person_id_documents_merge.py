"""Fusion id_documents → identity côté PERSONNE (parité société — la
même confusion tuée : un document officiel EST l'identité).

La taxonomie fiche person passe à 4 sections (identity · contact ·
situation · misc) ; les défs encore rangées en 'id_documents'
re-pointent 'identity'. Toute la logique vit dans
src/client_profiles/backfill.merge_person_id_documents_sections
(partagée migration/tests/protocole dump-prod). Idempotente, rejouable ;
le compte est logué. Sur base neuve, la taxonomie d'origine (a4c8) lit
le mapping du code vivant — déjà fusionné — et ce re-point est un no-op.

Revision ID: h8c2a6e0d4b8
Revises: g4b8d2f6c0a4
"""

from collections.abc import Sequence

from alembic import op

from src.client_profiles.backfill import merge_person_id_documents_sections

revision: str = "h8c2a6e0d4b8"
down_revision: str | None = "g4b8d2f6c0a4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    stats = merge_person_id_documents_sections(op.get_bind())
    print(f"person id_documents merge: {stats}")


def downgrade() -> None:
    # Migration de données à sens unique (la section id_documents n'existe
    # plus dans le code — la recréer n'aurait pas de sens). No-op assumé.
    pass

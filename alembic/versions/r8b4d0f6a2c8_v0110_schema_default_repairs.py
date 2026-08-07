"""D15 — les défauts serveur que le train v0.110.0 devait à la base.

L'INCIDENT : `agency_profile_section` est née (p3c7f1a9d5e8) avec
`created_at`/`updated_at` NOT NULL **sans server_default**, alors que
`TimestampMixin` est server_default-only — l'ORM n'envoie jamais de
valeur, il compte sur la base. La base de test naît des modèles (le
défaut y est), dev et prod naissent des migrations (il n'y était pas) :
`POST /agencies/me/profile-sections` mourait en NOT NULL violation sur
toute base migrée, invisible de la suite. Le seed des 64 lignes passait
parce que la migration fournissait now() explicitement.

Nouvelle révision, PAS une retouche de p3c7f1a9d5e8/q5e1a7c3f9b6 : elles
sont appliquées partout, on n'édite jamais une migration passée.

LE BALAYAGE DEMANDÉ (n9b5e3d7a1c6 + q5e1a7c3f9b6, toute colonne NOT NULL
sans server_default là où le modèle promet un défaut), verdict :
- `company_field_definition` : SAINE — timestamps hérités d'i6d0b2e8f4a2
  (server_default now()), toutes les colonnes ajoutées par n9b5e3d7a1c6
  portent leur défaut, q5e1a7c3f9b6 n'a aucun DDL ;
- `custom_field_definition` (au train par q5e1a7c3f9b6) : DEUX
  divergences préexistantes attrapées par la garde — `required` et
  `position` ont un défaut Python au modèle, rien en base. Pas la classe
  de l'incident (l'ORM envoie toujours ces valeurs, aucun 500 possible),
  mais la même dérive modèle↔migration : réparées ici, et le MODÈLE
  gagne les server_default correspondants dans le même lot — les deux
  vérités redisent la même chose, dans les deux sens.

Le test qui ferme la classe : tests/test_v0110_schema_defaults_migration
(schéma construit DEPUIS les migrations, insert minimal en SQL brut,
comparaison modèle↔base). Éprouvé ROUGE avant cette révision, VERT après.

Revision ID: r8b4d0f6a2c8
Revises: q5e1a7c3f9b6
"""

from alembic import op

revision = "r8b4d0f6a2c8"
down_revision = "q5e1a7c3f9b6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # L'incident — le POST /profile-sections repasse au 201.
    op.execute("ALTER TABLE agency_profile_section ALTER COLUMN created_at SET DEFAULT now()")
    op.execute("ALTER TABLE agency_profile_section ALTER COLUMN updated_at SET DEFAULT now()")
    # La classe — les deux divergences préexistantes de la même famille.
    op.execute("ALTER TABLE custom_field_definition ALTER COLUMN required SET DEFAULT false")
    op.execute('ALTER TABLE custom_field_definition ALTER COLUMN "position" SET DEFAULT 0')


def downgrade() -> None:
    op.execute('ALTER TABLE custom_field_definition ALTER COLUMN "position" DROP DEFAULT')
    op.execute("ALTER TABLE custom_field_definition ALTER COLUMN required DROP DEFAULT")
    op.execute("ALTER TABLE agency_profile_section ALTER COLUMN updated_at DROP DEFAULT")
    op.execute("ALTER TABLE agency_profile_section ALTER COLUMN created_at DROP DEFAULT")

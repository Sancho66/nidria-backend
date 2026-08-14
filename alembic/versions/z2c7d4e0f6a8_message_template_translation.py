"""Traduction IA des modèles de message (lot 14/08) — le rail s'élargit.

TROIS gestes, tous additifs :

1. `message_template.body_i18n` — le blob {lang: texte} des variantes, miroir
   exact des blobs de parcours (défaut '{}' : le parc existant démarre sans
   variante, le scalaire `body` reste la source que l'agence édite).
2. `ai_translation_job` : `template_id` devient NULLABLE et gagne son pendant
   `message_template_id` — UN job vise un parcours OU un modèle de message,
   jamais les deux, jamais aucun (CHECK num_nonnulls = 1). Une seule table
   parce que c'est UNE seule infra : même progression, même pool de points.
3. `ai_translation_source` : idem, plus l'index unique PARTIEL
   (message_template_id, content_key, lang) — le pendant du unique parcours,
   partiel parce que les lignes parcours portent message_template_id NULL.

Le CHECK est posé APRÈS coup en NOT VALID puis VALIDATE : les lignes
existantes sont toutes des lignes parcours (template_id non nul), donc la
validation passe sans réécrire la table — mais on ne fait pas confiance de
mémoire, on fait valider Postgres.

Revision ID: z2c7d4e0f6a8
Revises: y1b6c3d9e5f2
"""

import sqlalchemy as sa

from alembic import op

revision = "z2c7d4e0f6a8"
down_revision = "y1b6c3d9e5f2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "message_template",
        sa.Column(
            "body_i18n",
            sa.dialects.postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )

    for table in ("ai_translation_job", "ai_translation_source"):
        op.alter_column(table, "template_id", nullable=True)
        op.add_column(
            table,
            sa.Column("message_template_id", sa.Uuid(), nullable=True),
        )
        op.create_foreign_key(
            f"fk_{table}_message_template_id",
            table,
            "message_template",
            ["message_template_id"],
            ["id"],
            ondelete="CASCADE",
        )
        op.create_index(f"ix_{table}_message_template_id", table, ["message_template_id"])
        op.execute(
            f"ALTER TABLE {table} ADD CONSTRAINT ck_{table}_one_target "
            "CHECK (num_nonnulls(template_id, message_template_id) = 1) NOT VALID"
        )
        op.execute(f"ALTER TABLE {table} VALIDATE CONSTRAINT ck_{table}_one_target")

    op.create_index(
        "uq_ai_translation_source_message_key",
        "ai_translation_source",
        ["message_template_id", "content_key", "lang"],
        unique=True,
        postgresql_where=sa.text("message_template_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_ai_translation_source_message_key", table_name="ai_translation_source")
    for table in ("ai_translation_source", "ai_translation_job"):
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT ck_{table}_one_target")
        op.drop_index(f"ix_{table}_message_template_id", table_name=table)
        op.drop_constraint(f"fk_{table}_message_template_id", table, type_="foreignkey")
        # Les lignes MESSAGE deviendraient orphelines d'un template_id NOT
        # NULL : on les retire AVANT de re-serrer la colonne — le downgrade
        # ramène l'état d'avant le rail, il ne peut pas les garder.
        op.execute(f"DELETE FROM {table} WHERE template_id IS NULL")
        op.drop_column(table, "message_template_id")
        op.alter_column(table, "template_id", nullable=False)

    op.drop_column("message_template", "body_i18n")

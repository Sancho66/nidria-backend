"""La fiche société gagne de vraies définitions.

`company_field_label` ne portait qu'un libellé d'agence et le `kind` de
naissance d'une clé baptisée à la grille d'import. Elle devient LA table
de définitions de la face société : type, section, position, archivage,
libellé ×7. À partir de là, la fiche société lit des DÉFINITIONS et plus
un plan figé dans le code — archiver, renommer, reclasser et ranger
deviennent des gestes qui produisent quelque chose.

POURQUOI UNE TABLE DÉDIÉE PLUTÔT QUE `custom_field_definition` : la
contrainte `(agency_id, key)` y est UNIQUE, et 9 des 17 clés société
(`company_name`, `legal_form`, `share_capital`, `industry`,
`registration_date`, `company_registration_number`, `headquarters_address`,
`legal_representative_name`, `partners_count`) y existent DÉJÀ en
`scope='person'` — constat prod du 07/08, 3 agences. Une même clé ne peut
pas porter deux définitions. S'y ajouter aurait imposé un 3ᵉ scope, donc
un audit des ~20 appelants d'`active_definitions()` qui ne filtrent pas
le scope (cases_manager ×6, expat ×2, progress ×1) : les clés société
seraient devenues des champs dossier acceptés et affichés. Deux tables,
deux espaces de clés, zéro fuite.

LES DONNÉES EXISTANTES : les lignes déjà là sont des clés baptisées à la
grille d'import — elles prennent les défauts (`misc`, position 0, libellé
×7 vide), ce qui EST leur vérité. Aucune ligne en prod au 07/08 (vérifié
sur le dump), la reprise est donc gratuite ; elle ne le serait plus après
le prochain import société.

LES 17 PRESETS NE SONT PAS INSÉRÉS ICI. Ils se matérialisent
PARESSEUSEMENT à la première ouverture d'un écran société, agence par
agence (`materialize_company_definitions`) — le pattern de la face
personne. Une migration qui les sèmerait pour toutes les agences poserait
des lignes que personne n'a demandées, et rendrait le chemin paresseux
non testé donc mort.

Revision ID: n9b5e3d7a1c6
Revises: m7a3c9e1b5f4
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "n9b5e3d7a1c6"
down_revision = "m7a3c9e1b5f4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.rename_table("company_field_label", "company_field_definition")
    op.execute(
        "ALTER INDEX ix_company_field_label_agency_id"
        " RENAME TO ix_company_field_definition_agency_id"
    )
    # LES QUATRE NOMS, pas seulement celui de la table : la convention de
    # nommage du projet dérive index et contraintes du nom de table. En
    # laisser trois sur l'ancien nom, c'est garantir qu'un futur
    # `--autogenerate` propose de les « réparer » — un diff fantôme dans
    # une migration qui n'aura rien à voir. Vérifié sur le dump prod du
    # 07/08 : les trois portent exactement ces noms-là.
    op.execute(
        "ALTER TABLE company_field_definition"
        " RENAME CONSTRAINT uq_company_field_label TO uq_company_field_definition"
    )
    op.execute(
        "ALTER TABLE company_field_definition"
        " RENAME CONSTRAINT pk_company_field_label TO pk_company_field_definition"
    )
    op.execute(
        "ALTER TABLE company_field_definition RENAME CONSTRAINT"
        " fk_company_field_label_agency_id_agency"
        " TO fk_company_field_definition_agency_id_agency"
    )
    # `kind` → `field_type` : le même contrat de sortie sert les deux
    # faces, il doit parler une seule langue. Élargi à 20 pour accueillir
    # les types déjà servis côté société (`address`, `country`).
    op.alter_column(
        "company_field_definition",
        "kind",
        new_column_name="field_type",
        type_=sa.String(20),
        existing_type=sa.String(10),
        existing_nullable=False,
    )
    op.add_column(
        "company_field_definition",
        sa.Column(
            "label_i18n",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.add_column(
        "company_field_definition",
        sa.Column(
            "profile_section",
            sa.String(20),
            nullable=False,
            server_default="misc",
        ),
    )
    op.add_column(
        "company_field_definition",
        sa.Column("position", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )
    op.add_column(
        "company_field_definition",
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("company_field_definition", "archived_at")
    op.drop_column("company_field_definition", "position")
    op.drop_column("company_field_definition", "profile_section")
    op.drop_column("company_field_definition", "label_i18n")
    # Le retour à `kind` RETRÉCIT la colonne : les définitions typées
    # `address` (7 caractères) passent, mais le downgrade reste ce qu'il
    # est — un filet de déploiement, pas un aller-retour sans perte. Les
    # presets matérialisés redeviendraient de simples libellés.
    op.alter_column(
        "company_field_definition",
        "field_type",
        new_column_name="kind",
        type_=sa.String(10),
        existing_type=sa.String(20),
        existing_nullable=False,
    )
    op.execute(
        "ALTER TABLE company_field_definition RENAME CONSTRAINT"
        " fk_company_field_definition_agency_id_agency"
        " TO fk_company_field_label_agency_id_agency"
    )
    op.execute(
        "ALTER TABLE company_field_definition"
        " RENAME CONSTRAINT pk_company_field_definition TO pk_company_field_label"
    )
    op.execute(
        "ALTER TABLE company_field_definition"
        " RENAME CONSTRAINT uq_company_field_definition TO uq_company_field_label"
    )
    op.execute(
        "ALTER INDEX ix_company_field_definition_agency_id"
        " RENAME TO ix_company_field_label_agency_id"
    )
    op.rename_table("company_field_definition", "company_field_label")

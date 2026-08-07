"""Les sections de fiche deviennent une donnée d'agence.

Les 4 sections (identity / contact / situation / misc) vivaient dans
`PROFILE_SECTIONS`, en dur, identiques pour tout le monde. Elles vivent
désormais en base, par agence ET par surface (`person` | `company`).

CE QUE CETTE MIGRATION GARANTIT : **l'écran est identique le lendemain.**
Chaque agence existante reçoit ses 4 sections avec LEURS CLÉS INCHANGÉES,
dans l'ordre d'aujourd'hui. `custom_field_definition.profile_section` et
`company_field_definition.profile_section` continuent donc de pointer les
mêmes chaînes : aucun champ ne bouge, aucune valeur ne se perd.

`label_i18n` NAÎT VIDE, ET C'EST LE POINT DÉLICAT DU LOT. Vide ne veut pas
dire « sans nom » : il veut dire « l'agence n'a pas renommé », et le
libellé se résout alors depuis le catalogue produit, qui porte les 7
langues. Graver ici les libellés du jour aurait figé 8 agences × 4
sections × 7 langues sur l'état du 07/08 — elles n'auraient plus jamais
suivi une correction de traduction. Le repli coûte trois lignes
(`profile_sections_manager.section_name`) et évite ce gel.

LES AGENCES NÉES APRÈS cette migration ne sont pas oubliées : la lecture
matérialise paresseusement les 4 sections manquantes d'une surface
(`list_sections`), comme les champs société le font depuis le lot
précédent. La migration sert les agences EXISTANTES ; le chemin paresseux
sert les suivantes, et il est testé.

Revision ID: p3c7f1a9d5e8
Revises: n9b5e3d7a1c6
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "p3c7f1a9d5e8"
down_revision = "n9b5e3d7a1c6"
branch_labels = None
depends_on = None

# Les 4 clés + leur ordre, FIGÉS ICI : une migration ne lit pas le code
# applicatif, qui bougera. (Même règle que la table figée de m7a3c9e1b5f4.)
_SECTIONS = ("identity", "contact", "situation", "misc")
_SURFACES = ("person", "company")


def upgrade() -> None:
    op.create_table(
        "agency_profile_section",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("agency_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("surface", sa.String(10), nullable=False),
        sa.Column("key", sa.String(50), nullable=False),
        sa.Column(
            "label_i18n",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("position", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["agency_id"],
            ["agency.id"],
            name="fk_agency_profile_section_agency_id_agency",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_agency_profile_section"),
        sa.UniqueConstraint("agency_id", "surface", "key", name="uq_agency_profile_section"),
    )
    op.create_index(
        "ix_agency_profile_section_agency_id", "agency_profile_section", ["agency_id"]
    )
    op.execute('ALTER TABLE "agency_profile_section" ENABLE ROW LEVEL SECURITY')

    # Chaque agence existante × 2 surfaces × 4 sections, clés et ordre
    # d'aujourd'hui. `gen_random_uuid()` (pgcrypto, présent sur Supabase et
    # natif en PG13+) évite de faire un aller-retour Python par ligne.
    for surface in _SURFACES:
        for position, key in enumerate(_SECTIONS):
            op.execute(
                sa.text(
                    "INSERT INTO agency_profile_section"
                    " (id, agency_id, surface, key, label_i18n, position, created_at, updated_at)"
                    " SELECT gen_random_uuid(), a.id, :surface, :key, '{}'::jsonb, :position,"
                    " now(), now() FROM agency a"
                ).bindparams(surface=surface, key=key, position=position)
            )


def downgrade() -> None:
    op.drop_index("ix_agency_profile_section_agency_id", table_name="agency_profile_section")
    op.drop_table("agency_profile_section")

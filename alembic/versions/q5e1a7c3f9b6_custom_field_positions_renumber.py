"""D12 — les positions des définitions deviennent uniques, l'ordre stable.

LE DÉFAUT, constaté en dev le 07/08 : 4 entrées sur 12 à égalité de
position — `secondary_phone` et `website` partagent position ET
created_at à la microseconde, et l'`ORDER BY (position, created_at)` ne
départageait plus rien : l'ordre changeait tout seul entre deux lectures.

Deux réparations, indissociables :
1. LE CODE (même lot) : l'ORDER BY gagne `id` en dernier ressort — deux
   lectures ne peuvent plus rendre deux ordres, quoi qu'il y ait en base.
2. CETTE MIGRATION : renumérotation 1..N par agence, dans l'ordre
   actuellement SERVI (position, created_at, id — le même que le nouveau
   ORDER BY) : l'ordre observé ne change pas d'un cran, il devient
   unique. Les ARCHIVÉES passent après les actives — même invariant que
   le PUT /agencies/me/custom-fields/order posé dans ce lot : l'unicité
   tient dans toute la table, et une ressuscitée réapparaît en fin de
   liste plutôt qu'à un rang périmé.

La migration plutôt que le premier PUT : elle répare aussi les agences
qui ne réordonneront jamais.

Pas de downgrade des données — la renumérotation est une réparation, pas
un schéma : revenir aux égalités n'aurait aucun sens. Aucun DDL ici.

Constat société, une ligne : `company_field_definition` peut porter des
égalités mais son ORDER BY a déjà un tie-breaker stable (`key`, unique
par agence) — pas de défaut observable, la couverture par
`?surface=company` du PUT part en lot suivant si le besoin naît.

Revision ID: q5e1a7c3f9b6
Revises: p3c7f1a9d5e8
"""

import sqlalchemy as sa
from alembic import op

revision = "q5e1a7c3f9b6"
down_revision = "p3c7f1a9d5e8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Un seul UPDATE pour toutes les agences : actives d'abord (l'ordre
    # servi), archivées ensuite (le même ordre entre elles). Le garde
    # IS DISTINCT FROM rend la migration REJOUABLE à l'identique — la
    # seconde passe ne touche aucune ligne.
    op.execute(
        sa.text(
            "WITH ranked AS ("
            " SELECT id, row_number() OVER ("
            "  PARTITION BY agency_id"
            "  ORDER BY (archived_at IS NOT NULL), position, created_at, id"
            " ) AS rn FROM custom_field_definition"
            ") "
            "UPDATE custom_field_definition AS d SET position = r.rn"
            " FROM ranked AS r"
            " WHERE d.id = r.id AND d.position IS DISTINCT FROM r.rn"
        )
    )


def downgrade() -> None:
    # Rien à défaire : réparation de données, pas de schéma.
    pass

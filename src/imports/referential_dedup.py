"""Dédoublonnage du référentiel (lot dédup) — LA fonction de migration,
partagée migration/tests/protocole dump-prod (le précédent backfill).

Cas 1 — housing_address meurt, residence_address survit : valeurs de
sack renommées (règle de conflit MÊME LIGNE : le SURVIVANT gagne — le
« plus récent » est indéterminable par clé dans un sack JSONB, écart
nommé au rapport ; 0 cas en prod), déclarations de parcours re-pointées
(collision d'unicité → suppression), défs fusionnées (l'agence garde une
seule déf, renommée si besoin), configs d'import re-pointées.

Cas 2 — le preset preferred_language meurt, LA COLONNE preferred_lang
est la seule vérité : valeurs de sack fiche → colonne (normalisées par
la table de langues), valeurs de sack dossier → colonne de la fiche
LIÉE, clés retirées des sacks, déclarations supprimées (le rail cesse),
défs supprimées, configs re-pointées vers la colonne.

Idempotent, rejouable. Retourne les comptes (le rapport chiffré)."""

from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection


def dedup_referential(conn: Connection) -> dict[str, Any]:
    stats: dict[str, Any] = {}

    # --- Cas 1 : housing_address → residence_address ----------------------
    for table in ("case_person", "client_profile"):
        # conflit même ligne : le SURVIVANT gagne, le perdant compté.
        conflicts = conn.execute(
            text(
                f"SELECT count(*) FROM {table} WHERE custom_fields ? 'housing_address' "
                "AND custom_fields ? 'residence_address'"
            )
        ).scalar_one()
        stats[f"{table}_address_conflicts_survivor_kept"] = conflicts
        moved = conn.execute(
            text(
                f"UPDATE {table} SET custom_fields = (custom_fields - 'housing_address') "
                "|| jsonb_build_object('residence_address', custom_fields->'housing_address') "
                "WHERE custom_fields ? 'housing_address' "
                "AND NOT custom_fields ? 'residence_address'"
            )
        ).rowcount
        stats[f"{table}_address_values_moved"] = moved or 0
        conn.execute(
            text(
                f"UPDATE {table} SET custom_fields = custom_fields - 'housing_address' "
                "WHERE custom_fields ? 'housing_address'"
            )
        )

    # déclarations de parcours : re-point, collision d'unicité → delete.
    deleted = conn.execute(
        text(
            "DELETE FROM journey_template_field f USING journey_template_field g "
            "WHERE f.reference = 'housing_address' AND g.reference = 'residence_address' "
            "AND g.template_id = f.template_id AND g.kind = f.kind"
        )
    ).rowcount
    repointed = conn.execute(
        text(
            "UPDATE journey_template_field SET reference = 'residence_address' "
            "WHERE reference = 'housing_address'"
        )
    ).rowcount
    stats["journey_fields_address_repointed"] = repointed or 0
    stats["journey_fields_address_deleted_collision"] = deleted or 0

    # défs : fusion par agence (garde residence, sinon renomme housing).
    merged = conn.execute(
        text(
            "DELETE FROM custom_field_definition h USING custom_field_definition r "
            "WHERE h.key = 'housing_address' AND r.key = 'residence_address' "
            "AND r.agency_id = h.agency_id"
        )
    ).rowcount
    renamed = conn.execute(
        text(
            "UPDATE custom_field_definition SET key = 'residence_address', "
            "label = 'Adresse de résidence', profile_section = 'contact', scope = 'person' "
            "WHERE key = 'housing_address'"
        )
    ).rowcount
    stats["address_defs_merged"] = merged or 0
    stats["address_defs_renamed"] = renamed or 0

    # configs d'import : re-pointées (cibles sémantiquement équivalentes).
    cfg = conn.execute(
        text(
            "UPDATE crm_import_mapping SET mapping = replace(mapping::text, "
            "'\"housing_address', '\"residence_address')::jsonb "
            "WHERE mapping::text LIKE '%\"housing_address%'"
        )
    ).rowcount
    stats["import_configs_address_repointed"] = cfg or 0

    # --- Cas 2 : le preset preferred_language → LA COLONNE ---------------
    # La normalisation (accents compris) se fait en PYTHON sur les valeurs
    # DISTINCTES réelles — lower() SQL ne désaccentue pas ('Français').
    from src.imports.value_normalizers import normalize_language_code

    fiche_moved = 0
    distinct = conn.execute(
        text(
            "SELECT DISTINCT custom_fields->>'preferred_language' FROM client_profile "
            "WHERE custom_fields ? 'preferred_language'"
        )
    ).scalars()
    for raw in distinct:
        code = normalize_language_code(raw or "")
        if code is None:
            continue
        fiche_moved += (
            conn.execute(
                text(
                    "UPDATE client_profile SET preferred_lang = :code "
                    "WHERE preferred_lang IS NULL "
                    "AND custom_fields->>'preferred_language' = :raw"
                ),
                {"code": code, "raw": raw},
            ).rowcount
            or 0
        )
    person_moved = 0
    distinct = conn.execute(
        text(
            "SELECT DISTINCT custom_fields->>'preferred_language' FROM case_person "
            "WHERE custom_fields ? 'preferred_language'"
        )
    ).scalars()
    for raw in distinct:
        code = normalize_language_code(raw or "")
        if code is None:
            continue
        person_moved += (
            conn.execute(
                text(
                    "UPDATE client_profile p SET preferred_lang = :code "
                    "FROM case_person cp WHERE cp.client_profile_id = p.id "
                    "AND p.preferred_lang IS NULL "
                    "AND cp.custom_fields->>'preferred_language' = :raw"
                ),
                {"code": code, "raw": raw},
            ).rowcount
            or 0
        )
    stats["language_values_moved_from_fiche_sack"] = fiche_moved
    stats["language_values_moved_from_person_sack"] = person_moved
    unreadable = conn.execute(
        text(
            "SELECT count(*) FROM client_profile WHERE custom_fields ? 'preferred_language' "
            "AND preferred_lang IS NULL"
        )
    ).scalar_one()
    stats["language_values_unreadable_left_behind"] = unreadable
    for table in ("case_person", "client_profile"):
        conn.execute(
            text(
                f"UPDATE {table} SET custom_fields = custom_fields - 'preferred_language' "
                "WHERE custom_fields ? 'preferred_language'"
            )
        )
    stats["language_journey_fields_deleted"] = (
        conn.execute(
            text("DELETE FROM journey_template_field WHERE reference = 'preferred_language'")
        ).rowcount
        or 0
    )
    stats["language_defs_deleted"] = (
        conn.execute(
            text("DELETE FROM custom_field_definition WHERE key = 'preferred_language'")
        ).rowcount
        or 0
    )
    cfg = conn.execute(
        text(
            "UPDATE crm_import_mapping SET mapping = replace(mapping::text, "
            "'\"preferred_language\"', '\"preferred_lang\"')::jsonb "
            "WHERE mapping::text LIKE '%\"preferred_language\"%'"
        )
    ).rowcount
    stats["import_configs_language_repointed"] = cfg or 0
    return stats

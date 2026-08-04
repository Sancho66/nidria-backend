"""Backfill des fiches client (F1) — LA fonction, partagée par la
migration, les tests, et le protocole dump-prod (Phase 0 : la règle de
fusion écrite AVANT le code).

Règles (Phase 0, F verdict) :
- UNE fiche par (agency_id, expat_user_id) — les grappes multi-dossiers
  d'un même client fusionnent naturellement dans cette clé.
- LATEST-WINS PAR CHAMP : pour chaque champ civil et chaque clé custom, la
  valeur non-vide de la ligne case_person la plus récemment modifiée
  (updated_at) gagne ; les plus anciennes comblent les trous.
- Les personnes SANS compte (expat_user_id NULL — 7 en prod) restent hors
  fiche, liaison NULL, sans erreur.
- JAMAIS d'écriture inverse : les dossiers ne bougent pas d'un octet.
- IDEMPOTENT : une fiche existante n'est ni recréée ni modifiée ; les
  liaisons manquantes sont posées ; re-run = 0 création.

Sync (Connection SQLAlchemy) : appelable depuis alembic (op.get_bind())
et depuis les tests (run_sync)."""

from __future__ import annotations

import json
import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

CIVIL_COLUMNS = (
    "passport_number",
    "date_of_birth",
    "nationality",
    "place_of_birth",
    "sex",
    "marital_status",
    "phone",
    "birth_name",
    "profession",
    "employer",
)


def _is_empty(value: Any) -> bool:
    return value in (None, "", [], {})


def backfill_client_profiles(conn: Connection) -> dict[str, int]:
    """Crée les fiches manquantes et lie les case_person. Retourne les
    compteurs (rapport chiffré du protocole)."""
    rows = conn.execute(
        text(
            """
            SELECT cp.id AS person_id, cp.expat_user_id, cp.client_profile_id,
                   cp.updated_at, cc.agency_id,
                   cp.passport_number, cp.date_of_birth, cp.nationality,
                   cp.place_of_birth, cp.sex, cp.marital_status, cp.phone,
                   cp.birth_name, cp.profession, cp.employer,
                   cp.preferred_channels, cp.custom_fields
            FROM case_person cp
            JOIN client_case cc ON cc.id = cp.case_id
            WHERE cp.expat_user_id IS NOT NULL AND cc.deleted_at IS NULL
            ORDER BY cp.updated_at ASC, cp.id ASC
            """
        )
    ).mappings()

    # Groupement par (agency, expat) — l'ordre ASC fait que les lignes les
    # plus récentes ÉCRASENT en dernier (latest-wins par champ).
    groups: dict[tuple[uuid.UUID, uuid.UUID], dict[str, Any]] = {}
    person_ids_by_key: dict[tuple[uuid.UUID, uuid.UUID], list[uuid.UUID]] = {}
    unlinked_by_key: dict[tuple[uuid.UUID, uuid.UUID], int] = {}
    for row in rows:
        key = (row["agency_id"], row["expat_user_id"])
        merged = groups.setdefault(
            key, {c: None for c in CIVIL_COLUMNS} | {"preferred_channels": [], "custom_fields": {}}
        )
        for column in CIVIL_COLUMNS:
            if not _is_empty(row[column]):
                merged[column] = row[column]
        channels = row["preferred_channels"]
        if isinstance(channels, str):
            channels = json.loads(channels)
        if channels:
            merged["preferred_channels"] = list(dict.fromkeys(channels))
        custom = row["custom_fields"]
        if isinstance(custom, str):
            custom = json.loads(custom)
        for k, v in (custom or {}).items():
            if not _is_empty(v):
                merged["custom_fields"][k] = v
        person_ids_by_key.setdefault(key, []).append(row["person_id"])
        if row["client_profile_id"] is None:
            unlinked_by_key[key] = unlinked_by_key.get(key, 0) + 1

    stats = {
        "groups": len(groups),
        "profiles_created": 0,
        "profiles_existing": 0,
        "persons_linked": 0,
        "persons_without_account": 0,
        "merged_multi_case_groups": 0,
    }
    stats["persons_without_account"] = int(
        conn.execute(
            text(
                "SELECT count(*) FROM case_person cp JOIN client_case cc ON cc.id = cp.case_id "
                "WHERE cp.expat_user_id IS NULL AND cc.deleted_at IS NULL"
            )
        ).scalar_one()
    )

    for (agency_id, expat_user_id), merged in groups.items():
        existing = conn.execute(
            text(
                "SELECT id FROM client_profile "
                "WHERE agency_id = :agency_id AND expat_user_id = :expat_user_id"
            ),
            {"agency_id": agency_id, "expat_user_id": expat_user_id},
        ).scalar_one_or_none()
        if existing is not None:
            profile_id = existing
            stats["profiles_existing"] += 1
        else:
            profile_id = uuid.uuid4()
            conn.execute(
                text(
                    """
                    INSERT INTO client_profile (
                        id, agency_id, expat_user_id,
                        passport_number, date_of_birth, nationality, place_of_birth,
                        sex, marital_status, phone, birth_name, profession, employer,
                        preferred_channels, custom_fields, tags,
                        created_at, updated_at
                    ) VALUES (
                        :id, :agency_id, :expat_user_id,
                        :passport_number, :date_of_birth, :nationality, :place_of_birth,
                        :sex, :marital_status, :phone, :birth_name, :profession, :employer,
                        CAST(:preferred_channels AS jsonb), CAST(:custom_fields AS jsonb),
                        CAST('[]' AS jsonb), now(), now()
                    )
                    """
                ),
                {
                    "id": profile_id,
                    "agency_id": agency_id,
                    "expat_user_id": expat_user_id,
                    **{c: merged[c] for c in CIVIL_COLUMNS},
                    "preferred_channels": json.dumps(merged["preferred_channels"]),
                    "custom_fields": json.dumps(merged["custom_fields"]),
                },
            )
            stats["profiles_created"] += 1
        key = (agency_id, expat_user_id)
        if len(person_ids_by_key[key]) > 1:
            stats["merged_multi_case_groups"] += 1
        if unlinked_by_key.get(key):
            result = conn.execute(
                text(
                    "UPDATE case_person SET client_profile_id = :profile_id "
                    "WHERE id = ANY(:person_ids) AND client_profile_id IS NULL"
                ),
                {"profile_id": profile_id, "person_ids": person_ids_by_key[key]},
            )
            stats["persons_linked"] += result.rowcount or 0
    return stats


def merge_person_id_documents_sections(conn: Connection) -> dict[str, Any]:
    """Fusion id_documents → identity côté PERSONNE (parité société) —
    LA fonction, partagée migration/tests/protocole dump-prod : les défs
    encore rangées en 'id_documents' re-pointent 'identity'. Le code ne
    connaît plus cette section (taxonomie à 4) ; sans re-point, elles
    tomberaient en 'misc' au rendu. Idempotente, rejouable."""
    result = conn.execute(
        text(
            "UPDATE custom_field_definition SET profile_section = 'identity' "
            "WHERE profile_section = 'id_documents'"
        )
    )
    return {"definitions_repointed": result.rowcount or 0}

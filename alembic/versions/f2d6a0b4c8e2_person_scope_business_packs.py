"""Correctif final rail — reclassification des packs métier en 'person'.

LE fond du problème depuis trois passes : les catégories métier du rail
(Immigration, Logement, Fiscalité, Langue, Éducation, Véhicule,
Patrimoine, Professionnel) décrivent des TRAITS DU CLIENT, stables à
travers ses dossiers — leurs presets du catalogue passent scope='person'
(38 clés, tableau champ→verdict au rapport, véto d'Alexandre possible :
downgrade exact). Les exceptions restent 'case' : les dates de PROJET
(consular_appointment_date, target_school_start_date, move_in_date), les
souhaits de mission (target_tax_regime, tax_support_required,
property_type logement recherché), l'AML par opération (funds_origin),
et les catégories de mission entières (real_estate_deal,
consulting_mission). Les clés FAITES-MAIN des agences ne sont pas
touchées — le toggle scope reste leur juge.

Idempotente et rejouable (UPDATE par liste de clés, toutes agences).

Revision ID: f2d6a0b4c8e2
Revises: e6b0d2a4c8f6
"""

from collections.abc import Sequence

from alembic import op

revision: str = "f2d6a0b4c8e2"
down_revision: str | None = "e6b0d2a4c8f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Les 38 presets du catalogue reclassés 'person' (disjoints des 21 déjà
# 'person' depuis c8f2a6d0e4b8).
PERSON_PACK_KEYS = (
    # immigration — l'état COURANT du séjour décrit la personne
    "visa_type",
    "visa_number",
    "immigration_status",
    "residence_permit_number",
    "passport_expiry",
    "visa_permit_expiry",
    # professional — l'emploi actuel
    "job_title",
    "industry",
    "contract_type",
    "activity_status",
    "contract_start_date",
    # housing — la situation de logement ACTUELLE
    "housing_address",
    "housing_status",
    # tax — les identifiants et l'état fiscal de la personne
    "tax_residence_country",
    "tax_id",
    "origin_country_tax_filing",
    # language — les capacités linguistiques
    "host_language_level",
    "native_language",
    "other_languages",
    "interpreter_needed",
    # education — le parcours de formation
    "education_level",
    "last_institution",
    "field_of_study",
    "diploma_recognition",
    "children_school_preference",
    # vehicle — le véhicule et le permis de la personne
    "has_vehicle_to_import",
    "vehicle_make_model",
    "vehicle_registration_number",
    "driving_license_number",
    "license_to_exchange",
    "license_country",
    # wealth_review — le profil patrimonial du client
    "risk_profile",
    "wealth_objective",
    "investment_horizon",
    "estimated_wealth",
    "savings_capacity",
    "held_products",
    "esg_preference",
)


def _keys_sql() -> str:
    return ", ".join(f"'{k}'" for k in PERSON_PACK_KEYS)


def upgrade() -> None:
    op.execute(
        f"UPDATE custom_field_definition SET scope = 'person' WHERE key IN ({_keys_sql()})"
    )


def downgrade() -> None:
    op.execute(f"UPDATE custom_field_definition SET scope = 'case' WHERE key IN ({_keys_sql()})")

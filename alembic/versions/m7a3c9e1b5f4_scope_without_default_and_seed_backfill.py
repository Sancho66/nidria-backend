"""Le défaut silencieux meurt : portée obligatoire + rattrapage du seed.

CONSTAT du 06/08 (agence QuinnAshford) : `demo_case_seed`, l'import de
parcours et le script de seed créaient leurs définitions SANS portée ni
section. Elles tombaient donc sur les défauts de colonne ('case'/'misc'),
y compris pour des clés que le catalogue classe « personne » — invisibles
sur les fiches clients, sans que rien ne le signale. Toute agence créée
depuis le 04/08 en héritait.

Cette migration fait deux choses :

1. ELLE RETIRE LE DÉFAUT SERVEUR de `custom_field_definition.scope`.
   C'est l'arbitrage du 07/08 : un appelant qui oublie la portée doit
   ÉCHOUER à l'insert, bruyamment, dans la première passe de tests —
   plutôt que produire en silence des champs que personne ne verra avant
   qu'une agence se plaigne. Le code passe désormais la portée
   explicitement partout (`catalog_classification` pour les clés du
   catalogue). `profile_section` garde son défaut 'misc' : une section
   absente EST « divers », ce n'est pas un oubli porteur de bug.

2. ELLE RATTRAPE les définitions déjà produites par ce trou. Ciblage
   TRIPLE, mesuré sur la prod le 06/08 (19 lignes, agence QuinnAshford) :
   - la clé est un preset PERSON du catalogue (table figée ci-dessous —
     une migration ne lit pas le code applicatif, qui bougera) ;
   - scope='case' ET profile_section='misc' : la signature exacte des
     défauts de colonne. Une agence qui a délibérément posé un champ en
     « mission » depuis l'écran garde sa section (identity/contact/...),
     donc elle n'est PAS touchée ;
   - l'agence est née le 04/08 ou après, date du déploiement du correctif
     précédent (v0.100.0) — au-delà, un 'case' est un choix, pas un
     accident.

Revision ID: m7a3c9e1b5f4
Revises: k4f6a2c8e0d2
"""

from collections.abc import Sequence

from alembic import op

revision: str = "m7a3c9e1b5f4"
down_revision: str | None = "k4f6a2c8e0d2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Les 60 presets PERSON du catalogue et leur section, FIGÉS ici :
# une migration doit rejouer identique dans six mois, quoi que devienne
# `PRESET_PROFILE_SECTION` côté code.
PERSON_SECTIONS: dict[str, str] = {
    "activity_status": "situation",
    "birth_country": "identity",
    "children_count": "situation",
    "children_school_preference": "situation",
    "company_name": "situation",
    "company_registration_number": "situation",
    "contract_start_date": "situation",
    "contract_type": "situation",
    "dependents": "situation",
    "diploma_recognition": "situation",
    "driving_license_number": "identity",
    "education_level": "situation",
    "esg_preference": "situation",
    "estimated_wealth": "situation",
    "field_of_study": "situation",
    "has_vehicle_to_import": "situation",
    "headquarters_address": "situation",
    "held_products": "situation",
    "host_language_level": "situation",
    "housing_status": "situation",
    "immigration_status": "situation",
    "industry": "situation",
    "interpreter_needed": "situation",
    "investment_horizon": "situation",
    "job_title": "situation",
    "last_institution": "situation",
    "legal_form": "situation",
    "legal_representative_name": "situation",
    "license_country": "identity",
    "license_to_exchange": "identity",
    "linkedin_url": "contact",
    "matrimonial_regime": "situation",
    "native_language": "situation",
    "origin_country_tax_filing": "situation",
    "other_languages": "situation",
    "partners_count": "situation",
    "passport_expiry": "identity",
    "preferred_contact_channel": "contact",
    "registration_date": "situation",
    "residence_address": "contact",
    "residence_permit_number": "identity",
    "risk_profile": "situation",
    "savings_capacity": "situation",
    "second_nationality": "identity",
    "secondary_email": "contact",
    "secondary_phone": "contact",
    "share_capital": "situation",
    "spouse_birth_date": "situation",
    "spouse_name": "situation",
    "spouse_nationality": "situation",
    "tax_id": "identity",
    "tax_residence_country": "situation",
    "vehicle_make_model": "situation",
    "vehicle_registration_number": "situation",
    "visa_number": "identity",
    "visa_permit_expiry": "identity",
    "visa_type": "identity",
    "wealth_objective": "situation",
    "website": "contact",
    "whatsapp": "contact",
}

# Le correctif précédent (v0.100.0) est parti le 04/08 à 10:22 UTC. Une
# agence née avant a pu classer ses champs elle-même depuis.
FIX_DEPLOYED_AT = "2026-08-04"


def upgrade() -> None:
    # 1. Le défaut silencieux disparaît (la colonne reste NOT NULL).
    op.execute("ALTER TABLE custom_field_definition ALTER COLUMN scope DROP DEFAULT")

    # 2. Le rattrapage, section par section (un UPDATE par section : les
    #    trois listes sont courtes et le SQL reste lisible).
    by_section: dict[str, list[str]] = {}
    for key, section in PERSON_SECTIONS.items():
        by_section.setdefault(section, []).append(key)
    for section, keys in sorted(by_section.items()):
        keys_sql = ", ".join(f"'{k}'" for k in sorted(keys))
        op.execute(
            "UPDATE custom_field_definition d "
            f"SET scope = 'person', profile_section = '{section}' "
            "FROM agency a "
            "WHERE a.id = d.agency_id "
            f"AND d.key IN ({keys_sql}) "
            "AND d.scope = 'case' AND d.profile_section = 'misc' "
            f"AND a.created_at >= '{FIX_DEPLOYED_AT}'"
        )


def downgrade() -> None:
    # Le défaut revient (sinon tout insert historique casserait), mais le
    # rattrapage NE se défait PAS : on ne re-cache pas des champs qu'une
    # agence voit désormais sur ses fiches.
    op.execute("ALTER TABLE custom_field_definition ALTER COLUMN scope SET DEFAULT 'case'")

"""La taxonomie FICHE (lot taxonomie) — les sections PROPRES de la fiche.

Deux univers assumés : le picker de collecte garde SES catégories
(`SECTION_TYPES`, le rail) ; la fiche sert LES SIENNES — QUATRE sections
stables (fusion id_documents → identity, parité personne/société : un
numéro officiel EST l'identité), i18n ×7. Le contrat n'est plus
l'égalité picker==fiche mais l'EXHAUSTIVITÉ : tout champ person a
exactement UNE profile_section (les colonnes civiles par le mapping
code ci-dessous, les définitions custom par leur colonne
`profile_section`, défaut 'misc').

Posée AUSSI pour F5 (fiches société) : `COMPANY_PROFILE_SECTIONS`
réutilise la même taxonomie — F5 n'invente rien.
"""

from typing import Final

# Ordre d'affichage = ordre de déclaration.
PROFILE_SECTIONS: Final[dict[str, dict[str, str]]] = {
    "identity": {
        "fr": "Identité",
        "en": "Identity",
        "es": "Identidad",
        "ru": "Личные данные",
        "pt": "Identidade",
        "it": "Identità",
        "hu": "Személyazonosság",
    },
    "contact": {
        "fr": "Contact",
        "en": "Contact",
        "es": "Contacto",
        "ru": "Контакты",
        "pt": "Contacto",
        "it": "Contatti",
        "hu": "Kapcsolat",
    },
    "situation": {
        "fr": "Situation",
        "en": "Situation",
        "es": "Situación",
        "ru": "Ситуация",
        "pt": "Situação",
        "it": "Situazione",
        "hu": "Élethelyzet",
    },
    "misc": {
        "fr": "Divers",
        "en": "Miscellaneous",
        "es": "Varios",
        "ru": "Прочее",
        "pt": "Diversos",
        "it": "Varie",
        "hu": "Egyéb",
    },
}

# Fiches société : la MÊME taxonomie à 4 — la parité est revenue (la
# fusion id_documents → identity vaut désormais des deux côtés).
COMPANY_PROFILE_SECTIONS: Final[dict[str, dict[str, str]]] = PROFILE_SECTIONS

# V2b — le plan de valeurs SOCIÉTÉ sur la taxonomie : les presets
# company du catalogue → leur section de fiche société. Les clés libres
# d'agence tombent en 'misc'.
COMPANY_PRESET_PROFILE_SECTION: Final[dict[str, str]] = {
    # identité légale
    "company_name": "identity",
    "legal_form": "identity",
    "registration_date": "identity",
    # numéros officiels — dans l'IDENTITÉ LÉGALE (fusion id_documents)
    "company_registration_number": "identity",  # Siret/EIK
    "vat_number": "identity",
    # contact
    "headquarters_address": "contact",
    "legal_representative_name": "contact",
    "country": "contact",
    "email": "contact",
    "phone": "contact",
    "website": "contact",
    "address": "contact",  # texte intégral (la règle connue — pas de parsing)
    # activité (audit catalogue : industry/effectif triple source
    # TL+HubSpot+Pipedrive ; activity_code = registre NACE, fichiers Nico)
    "share_capital": "situation",
    "partners_count": "situation",
    "industry": "situation",
    "employee_count": "situation",
    "activity_code": "situation",
}

# Cibles company NUMÉRIQUES : coercées en number à l'import (la règle
# « suggérable = coerçable » — échec de cellule = issue + trou, jamais 500).
COMPANY_NUMBER_TARGETS: Final[tuple[str, ...]] = ("employee_count", "share_capital")

# Demande design A (03/08) : l'univers SOCIÉTÉ quitte la fiche PERSONNE.
# Ces presets restent des champs de COLLECTE dossier (la section
# « Création de société » des parcours — 33 déclarations en prod) et
# vivent sur la fiche SOCIÉTÉ (COMPANY_PRESET_PROFILE_SECTION) ; la
# fiche personne ne les sert plus : sections, complétude, divergences,
# cibles et suggestions d'import personne. Constat prod 03/08 : chaque
# valeur fiche de ces clés est le miroir exact d'une valeur dossier —
# rien ne devient invisible qui ne soit déjà servi par la collecte.
PERSON_SHEET_EXCLUDED_KEYS: Final[frozenset[str]] = frozenset(
    {
        "company_name",
        "legal_form",
        "share_capital",
        "company_registration_number",
        "registration_date",
        "headquarters_address",
        "legal_representative_name",
        "partners_count",
    }
)

# Alias d'import acceptés → clé canonique (compat : une seule vérité par
# concept ; `registration_number` demandé = `company_registration_number`).
COMPANY_TARGET_ALIASES: Final[dict[str, str]] = {
    "registration_number": "company_registration_number",
}

# L'ordre INTERNE de la section identity fusionnée : l'état civil
# d'abord, les documents ensuite (l'ordre du catalogue). Les clés hors
# liste (customs d'agence rangés en identity par le toggle) suivent,
# dans leur ordre d'arrivée — tri stable.
IDENTITY_SECTION_ORDER: Final[tuple[str, ...]] = (
    # état civil
    "date_of_birth",
    "nationality",
    "place_of_birth",
    "sex",
    "birth_name",
    "birth_country",
    "second_nationality",
    # documents (l'ordre du catalogue)
    "passport_number",
    "passport_expiry",
    "visa_type",
    "visa_number",
    "visa_permit_expiry",
    "residence_permit_number",
    "driving_license_number",
    "license_country",
    "license_to_exchange",
    "tax_id",
)

# Les 10 colonnes civiles natives → leur section fiche (mapping code :
# ce ne sont pas des lignes custom_field_definition).
#
# BACKLOG D5 — surcharge par agence de cette table. `Final` est un choix,
# pas un oubli : une colonne civile n'a pas de définition, donc pas de
# `profile_section` à éditer ; la rendre configurable demande une table
# d'override par agence, lue partout où cette constante l'est
# aujourd'hui (fiche, univers affiché, complétude, collecte).
# CONDITION D'OUVERTURE : une agence demande EXPLICITEMENT de déplacer
# une colonne civile dans une autre section. Tant que personne ne l'a
# demandé, l'infobulle de l'écran explique pourquoi c'est fixe — c'est
# moins cher qu'une table d'override que personne n'utilise.
CIVIL_PROFILE_SECTION: Final[dict[str, str]] = {
    "date_of_birth": "identity",
    "nationality": "identity",
    "place_of_birth": "identity",
    "sex": "identity",
    "birth_name": "identity",
    "passport_number": "identity",  # fusion id_documents → identity
    "phone": "contact",
    "marital_status": "situation",
    "profession": "situation",
    "employer": "situation",
}

# Les presets person du catalogue → leur section fiche. Source du
# backfill de migration (les custom nés d'agence restent 'misc',
# reclassables par le toggle élargi). Tableau champ→section au rapport.
PRESET_PROFILE_SECTION: Final[dict[str, str]] = {
    # identity — l'état civil…
    "birth_country": "identity",
    "second_nationality": "identity",
    # …puis les numéros et titres officiels (fusion id_documents →
    # identity : un document officiel EST l'identité, parité société)
    "passport_expiry": "identity",
    "visa_type": "identity",
    "visa_number": "identity",
    "visa_permit_expiry": "identity",
    "residence_permit_number": "identity",
    "driving_license_number": "identity",
    "license_country": "identity",
    "license_to_exchange": "identity",
    "tax_id": "identity",
    # contact
    "residence_address": "contact",
    "secondary_phone": "contact",  # la maison du Mobile (correctif import a)
    "secondary_email": "contact",
    "whatsapp": "contact",
    "website": "contact",  # audit catalogue : présence en ligne (TL + HubSpot)
    "linkedin_url": "contact",  # audit catalogue : le seul réseau métier
    "preferred_contact_channel": "contact",
    # situation — l'état de vie courant (famille, société, emploi,
    # logement, fiscalité, langue, scolarité, véhicule, patrimoine)
    "spouse_name": "situation",
    "spouse_nationality": "situation",
    "spouse_birth_date": "situation",
    "children_count": "situation",
    "dependents": "situation",
    "matrimonial_regime": "situation",
    # thème company : matérialisés scope person POUR LA COLLECTE dossier,
    # mais ÉCARTÉS de la fiche personne (PERSON_SHEET_EXCLUDED_KEYS).
    "company_name": "situation",
    "legal_form": "situation",
    "share_capital": "situation",
    "company_registration_number": "situation",
    "registration_date": "situation",
    "headquarters_address": "situation",
    "legal_representative_name": "situation",
    "partners_count": "situation",
    "immigration_status": "situation",
    "job_title": "situation",
    "industry": "situation",
    "contract_type": "situation",
    "activity_status": "situation",
    "contract_start_date": "situation",
    "housing_status": "situation",
    "tax_residence_country": "situation",
    "origin_country_tax_filing": "situation",
    "host_language_level": "situation",
    "native_language": "situation",
    "other_languages": "situation",
    "interpreter_needed": "situation",
    "education_level": "situation",
    "last_institution": "situation",
    "field_of_study": "situation",
    "diploma_recognition": "situation",
    "children_school_preference": "situation",
    "has_vehicle_to_import": "situation",
    "vehicle_make_model": "situation",
    "vehicle_registration_number": "situation",
    "risk_profile": "situation",
    "wealth_objective": "situation",
    "investment_horizon": "situation",
    "estimated_wealth": "situation",
    "savings_capacity": "situation",
    "held_products": "situation",
    "esg_preference": "situation",
}


def catalog_classification(key: str) -> tuple[str, str]:
    """(scope, profile_section) d'une clé — LA règle, écrite UNE fois.

    Présente dans `PRESET_PROFILE_SECTION` = trait de personne, rangé dans
    sa section ; absente (clé faite-main ou preset de mission) = propre au
    dossier, section « divers ».

    Elle existait en trois exemplaires recopiés, et deux appelants sur
    cinq l'avaient oubliée : `demo_case_seed` et l'import de parcours
    créaient leurs définitions sur les défauts de colonne, donc en
    « mission »/« divers » même pour des clés que le catalogue classe
    « personne ». Constat du 06/08, agence QuinnAshford. Tout appelant
    qui matérialise un preset passe désormais par ici.
    """
    section = PRESET_PROFILE_SECTION.get(key)
    return ("person", section) if section is not None else ("case", "misc")

"""La taxonomie FICHE (lot taxonomie) — les sections PROPRES de la fiche.

Deux univers assumés : le picker de collecte garde SES catégories
(`SECTION_TYPES`, le rail) ; la fiche sert LES SIENNES — cinq sections
stables, i18n ×7. Le contrat n'est plus l'égalité picker==fiche mais
l'EXHAUSTIVITÉ : tout champ person a exactement UNE profile_section
(les colonnes civiles par le mapping code ci-dessous, les définitions
custom par leur colonne `profile_section`, défaut 'misc').

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
    "id_documents": {
        "fr": "Documents d'identité",
        "en": "Identity documents",
        "es": "Documentos de identidad",
        "ru": "Документы",
        "pt": "Documentos de identidade",
        "it": "Documenti d'identità",
        "hu": "Személyes okmányok",
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

# F5 (fiches société) : la MÊME taxonomie, nommée dès maintenant.
COMPANY_PROFILE_SECTIONS: Final[dict[str, dict[str, str]]] = PROFILE_SECTIONS

# V2b — le plan de valeurs SOCIÉTÉ sur la taxonomie : les 8 presets
# company du catalogue → leur section de fiche société. Les clés libres
# d'agence tombent en 'misc'.
COMPANY_PRESET_PROFILE_SECTION: Final[dict[str, str]] = {
    # identité légale
    "company_name": "identity",
    "legal_form": "identity",
    "registration_date": "identity",
    # numéros officiels (audit import : les cibles de tout export réel)
    "company_registration_number": "id_documents",  # Siret/EIK
    "vat_number": "id_documents",
    # contact
    "headquarters_address": "contact",
    "legal_representative_name": "contact",
    "country": "contact",
    "email": "contact",
    "phone": "contact",
    "website": "contact",
    "address": "contact",  # texte intégral (la règle connue — pas de parsing)
    # activité
    "share_capital": "situation",
    "partners_count": "situation",
}

# Alias d'import acceptés → clé canonique (compat : une seule vérité par
# concept ; `registration_number` demandé = `company_registration_number`).
COMPANY_TARGET_ALIASES: Final[dict[str, str]] = {
    "registration_number": "company_registration_number",
}

# Les 10 colonnes civiles natives → leur section fiche (mapping code :
# ce ne sont pas des lignes custom_field_definition).
CIVIL_PROFILE_SECTION: Final[dict[str, str]] = {
    "date_of_birth": "identity",
    "nationality": "identity",
    "place_of_birth": "identity",
    "sex": "identity",
    "birth_name": "identity",
    "passport_number": "id_documents",
    "phone": "contact",
    "marital_status": "situation",
    "profession": "situation",
    "employer": "situation",
}

# Les presets person du catalogue → leur section fiche. Source du
# backfill de migration (les custom nés d'agence restent 'misc',
# reclassables par le toggle élargi). Tableau champ→section au rapport.
PRESET_PROFILE_SECTION: Final[dict[str, str]] = {
    # identity
    "birth_country": "identity",
    "second_nationality": "identity",
    # contact
    "residence_address": "contact",
    "secondary_email": "contact",
    "whatsapp": "contact",
    "preferred_language": "contact",
    "preferred_contact_channel": "contact",
    # id_documents — les numéros et titres officiels de la personne
    "passport_expiry": "id_documents",
    "visa_type": "id_documents",
    "visa_number": "id_documents",
    "visa_permit_expiry": "id_documents",
    "residence_permit_number": "id_documents",
    "driving_license_number": "id_documents",
    "license_country": "id_documents",
    "license_to_exchange": "id_documents",
    "tax_id": "id_documents",
    # situation — l'état de vie courant (famille, société, emploi,
    # logement, fiscalité, langue, scolarité, véhicule, patrimoine)
    "spouse_name": "situation",
    "spouse_nationality": "situation",
    "spouse_birth_date": "situation",
    "children_count": "situation",
    "dependents": "situation",
    "matrimonial_regime": "situation",
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
    "housing_address": "situation",
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

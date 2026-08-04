"""La TABLE D'ALIAS des en-têtes d'import (lot mapping complet).

Chaque en-tête des exports réels (Teamleader Contacts 42 + Companies 54)
a un VERDICT : aliasé vers une cible, ou EXCLU MOTIVÉ (le tableau
complet vit au rapport du lot ; les raisons d'exclusion sont ici en
commentaire — le code est la source).

Le matching est à trois étages, dans l'ordre :
1. EXCLUSIONS d'abord (les faux amis ne sont JAMAIS suggérés —
   « Adresse e-mail facturation » ne doit pas accrocher email en fuzzy) ;
2. alias EXACTS normalisés (FR/EN du vocabulaire CRM courant) ;
3. repli (fallback — Mobile→phone si aucune colonne téléphone directe),
   clés de défs d'agence (dynamique), puis fuzzy PRUDENT (difflib ≥0.85).
Une cible n'est suggérée qu'UNE fois (la première colonne gagne)."""

import difflib
import unicodedata
from collections.abc import Mapping
from typing import Final


def normalize_header(header: str) -> str:
    """minuscules, accents retirés, ponctuation → espaces, trim."""
    s = unicodedata.normalize("NFKD", header)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = "".join(c if c.isalnum() else " " for c in s.lower())
    return " ".join(s.split())


# LE COUPLE rue + numéro (l'exception déclarée à l'anti-concaténation) :
# ces en-têtes désignent le NUMÉRO de rue — suggérés vers le MÊME
# <base>.street que la colonne rue quand les deux existent, et reconnus à
# l'assemblage pour l'ordre fixe « {numéro} {rue} ». Jamais suggérés
# seuls (un numéro sans rue n'est pas une adresse).
STREET_NUMBER_HEADERS: Final[frozenset[str]] = frozenset(
    normalize_header(h)
    for h in (
        "Numéro de la rue",
        "Numéro de rue",
        "Numéro rue",
        "Street number",
        "House number",
        "House no",
    )
)

# --- PERSONNES ------------------------------------------------------------------------

# En-têtes JAMAIS suggérés (faux amis et hors-cible volontaires).
PERSON_EXCLUDED: Final[frozenset[str]] = frozenset(
    normalize_header(h)
    for h in (
        "Teamleader ID",  # identifiant CRM source
        "Province",  # aucun sous-champ adresse correspondant
        "Opt-in courriers marketing",  # préférence marketing CRM
        "Fax",  # obsolète
        "Actif",  # état CRM
        "Liste des prix",  # facturation CRM
        "Nombre de minutes non facturées",
        "Entreprises",  # multivalué, rattachement société hors v1
        "Sous-fonction",
        "Décideur",  # aucune cible fiche — volontaire
        "Dernière activité",  # métadonnées CRM (l'activité Nidria se dérive)
        "Dernier rendez-vous",
        "Date ajoutée",
        "Dernière modification",
        "Crédits prépayés restants",
        "N° Compte IBAN",  # bancaire — pas de cible fiche v1
        "Code BIC",
        "Conditions de paiement",
        "Total à facturer",
        "ID externe",
        "Traçage prospects",
        "Taux horaire",
        # audit catalogue (vocabulaire HubSpot) :
        "Relationship status",  # doublon flou de marital_status, valeurs incoerçables
        "State",  # le Province EN — aucun sous-champ adresse correspondant
        "State/Region",
        "Number of employees",  # donnée SOCIÉTÉ enrichie sur la fiche personne
        "Annual revenue",
        # demande design A (03/08) : l'univers société a quitté la fiche
        # personne — le Siret d'un CONTACT suit la même règle que Number
        # of employees (donnée société, l'import sociétés la porte).
        "Numéro d'identification national (SIRET)",
        "Siret",
    )
)

# Alias exacts normalisés → cible. FR (Teamleader) + EN courant CRM.
PERSON_ALIASES: Final[dict[str, str]] = {
    # identité
    "prenom": "first_name",
    "first name": "first_name",
    "firstname": "first_name",
    "given name": "first_name",
    "nom de famille": "last_name",
    "nom": "last_name",
    "last name": "last_name",
    "lastname": "last_name",
    "surname": "last_name",
    "family name": "last_name",
    # email
    "adresse e mail": "email",
    "adresse email": "email",
    "email": "email",
    "e mail": "email",
    "courriel": "email",
    "mail": "email",
    "email address": "email",
    # téléphone
    "telephone": "phone",
    "phone": "phone",
    "phone number": "phone",
    "tel": "phone",
    # civil
    "date de naissance": "date_of_birth",
    "naissance": "date_of_birth",
    "date of birth": "date_of_birth",
    "birth date": "date_of_birth",
    "birthdate": "date_of_birth",
    "birthday": "date_of_birth",
    "dob": "date_of_birth",
    "genre": "sex",
    "sexe": "sex",
    "gender": "sex",
    "nationalite": "nationality",
    "nationality": "nationality",
    "lieu de naissance": "place_of_birth",
    "birthplace": "place_of_birth",
    "place of birth": "place_of_birth",
    "etat civil": "marital_status",
    "situation matrimoniale": "marital_status",
    "marital status": "marital_status",
    "passeport": "passport_number",
    "passport": "passport_number",
    "passport number": "passport_number",
    "numero de passeport": "passport_number",
    "profession": "profession",
    "metier": "profession",
    "fonction": "profession",  # la fonction d'un contact = sa profession
    "job title": "profession",
    "poste": "profession",
    "employeur": "employer",
    "employer": "employer",
    "societe": "employer",  # la société d'un CONTACT = son employeur
    "company": "employer",
    "company name": "employer",
    "nom de naissance": "birth_name",
    "maiden name": "birth_name",
    # adresse : TEXTE INTÉGRAL pour les colonnes complètes…
    "adresse postale": "residence_address",
    "adresse": "residence_address",
    "adresse complete": "residence_address",
    "address": "residence_address",
    "full address": "residence_address",
    # …et SOUS-CHAMPS pour les fragments (composition visible — l'anti-
    # parsing ne vaut que dans l'AUTRE sens : jamais découper une colonne).
    "rue": "residence_address.street",
    "street": "residence_address.street",
    "street address": "residence_address.street",
    "ville": "residence_address.city",
    "city": "residence_address.city",
    "town": "residence_address.city",
    "code postal": "residence_address.postal_code",
    "postal code": "residence_address.postal_code",
    "zip": "residence_address.postal_code",
    "zip code": "residence_address.postal_code",
    # la LANGUE vise LA COLONNE fiche (dédoublonnage — le preset est
    # mort : la colonne pilote notifications et hero, seule vérité)
    "langue": "preferred_lang",
    "language": "preferred_lang",
    "langue preferee": "preferred_lang",
    "preferred language": "preferred_lang",
    # défs person courantes (suggérées si la déf existe chez l'agence)
    "whatsapp": "whatsapp",
    # présence en ligne (audit catalogue : TL « Site web » + HubSpot)
    "site web": "website",
    "website": "website",
    "site internet": "website",
    "website url": "website",
    "linkedin": "linkedin_url",
    "linkedin url": "linkedin_url",
    "profil linkedin": "linkedin_url",
    "linkedin profile": "linkedin_url",
    # éducation / vie professionnelle (vocabulaire HubSpot → presets)
    "degree": "education_level",
    "diplome": "education_level",
    "school": "last_institution",
    "ecole": "last_institution",
    "etablissement": "last_institution",
    "field of study": "field_of_study",
    "domaine d etudes": "field_of_study",
    "industry": "industry",
    "secteur": "industry",
    "secteur d activite": "industry",
    # email secondaire (aujourd'hui muet — audit catalogue)
    "work email": "secondary_email",
    "secondary email": "secondary_email",
    "email secondaire": "secondary_email",
    "second email": "secondary_email",
    "email 2": "secondary_email",
    "e mail 2": "secondary_email",
    # numéros officiels (verdicts actés au lot plafond)
    "numero de tva du contact": "tax_id",  # la TVA d'un contact = son NIF
    "vat": "tax_id",
    "vat number": "tax_id",
    # (re-verdict 03/08 : Siret contact = donnée société — l'univers
    # société a quitté la fiche personne, l'alias part en exclusion)
    # fiche
    "tags": "tags",
    "etiquettes": "tags",
    "labels": "tags",
    "label": "tags",  # le label Pipedrive = un tag
}

# AMBIGUÏTÉ OFFERTE, jamais devinée : ces en-têtes proposent PLUSIEURS
# cibles au combobox — rien d'auto-posé.
PERSON_AMBIGUOUS: Final[dict[str, list[str]]] = {
    # TROIS lectures — nationalité, résidence fiscale, pays de l'adresse :
    # le choix est explicite, rien ne se devine.
    "pays": ["nationality", "tax_residence_country", "residence_address.country"],
    "country": ["nationality", "tax_residence_country", "residence_address.country"],
    "country region": ["nationality", "tax_residence_country", "residence_address.country"],
    "pays de residence": ["tax_residence_country", "residence_address.country", "nationality"],
}

# Replis : suggérés SEULEMENT si la cible n'a pas déjà de colonne directe.
# Une valeur TUPLE est une CHAÎNE de replis, essayée dans l'ordre — le
# mobile a désormais un second cran (`secondary_phone`) au lieu du vide
# quand une colonne Téléphone tient déjà `phone` (correctif import a :
# 235 contacts sans numéro sur le fichier Teamleader réel).
PERSON_FALLBACK_ALIASES: Final[dict[str, str | tuple[str, ...]]] = {
    "mobile": ("phone", "secondary_phone"),
    "cell": ("phone", "secondary_phone"),
    "cell phone": ("phone", "secondary_phone"),
    "portable": ("phone", "secondary_phone"),
    "gsm": ("phone", "secondary_phone"),
    "mobile phone": ("phone", "secondary_phone"),
    "mobile phone number": ("phone", "secondary_phone"),
    "telephone portable": ("phone", "secondary_phone"),
    "telephone mobile": ("phone", "secondary_phone"),
    # civilité : REPLI — si une colonne Genre directe existe, elle gagne
    # (le normaliseur de valeurs traite M./Mme/Mr).
    "civilite": "sex",
    "salutation": "sex",
}

# --- SOCIÉTÉS -------------------------------------------------------------------------

COMPANY_EXCLUDED: Final[frozenset[str]] = frozenset(
    normalize_header(h)
    for h in (
        "Teamleader ID",
        "Province",
        "Langue",  # pas de cible langue société
        "Adresse e-mail facturation",  # FAUX AMI de email (facturation)
        "TVA",  # le TAUX, pas le numéro — faux ami de vat_number
        "Gestionnaire de compte",  # agent CRM
        "Actif",
        "Fax",  # obsolète (déjà la règle person)
        "Client COMPTA",  # colonnes métier de l'agence (sack libre)
        "Comptable",
        "Date of VAT Reg",
        "end contrat Dom",
        "NUM IRINA",
        "second Email",
        "Liste des prix",
        "Conditions de paiement",
        "Total à facturer",
        "ID externe",
        "Nombre de minutes non facturées",
        "Dernière activité",
        "Dernier rendez-vous",
        "Date ajoutée",
        "Dernière modification",
        "Entreprises associées",
        "Crédits prépayés restants",
        "N° Compte IBAN",
        "Code BIC",
        "Notation",  # le pack finance CRM (« # Collaborateurs » en est
        "Chiffre d'affaires",  # sorti — un effectif n'est pas un ratio)
        "Marge bén. br.",
        "Bénéfice",
        "Quick ratio",
        "Degré ind. fin.",
        "Valeur ajoutée",
        "Valeur ajout. par collaborateur",
        "ROE",
        "Taux horaire",
        "Opt-in courriers marketing",
        # audit catalogue (vocabulaire HubSpot) :
        "Employee range",  # plage enrichie (« 51-200 ») — pas un nombre
        "Revenue range",
        "Annual revenue",  # l'EN de « Chiffre d'affaires » — même pack finance
        "Year founded",  # année seule — pas coerçable en date sans inventer
        "State",  # le Province EN — aucun sous-champ correspondant
        "State/Region",
        "Type",  # taxonomie CRM (prospect/partner) — les tags couvrent
        "Description",  # texte libre CRM
        "About us",
        "LinkedIn company page",  # véto audit — le site suffit
    )
)

COMPANY_ALIASES: Final[dict[str, str]] = {
    "nom": "name",
    "name": "name",
    "company": "name",
    "company name": "name",
    "societe": "name",
    "raison sociale": "name",
    "denomination": "name",
    "numero de tva": "vat_number",
    "vat": "vat_number",
    "vat number": "vat_number",
    "tva intracommunautaire": "vat_number",
    "pays": "country",
    "country": "country",
    "country region": "country",
    "adresse e mail": "email",
    "adresse email": "email",
    "email": "email",
    "e mail": "email",
    "courriel": "email",
    "telephone": "phone",
    "phone": "phone",
    "phone number": "phone",
    "tel": "phone",
    "site web": "website",
    "website": "website",
    "website url": "website",
    "web": "website",
    "site internet": "website",
    "numero d identification national siret": "company_registration_number",
    "siret": "company_registration_number",
    "siren": "company_registration_number",
    "eik": "company_registration_number",
    "registration number": "company_registration_number",
    "numero d immatriculation": "company_registration_number",
    "type d entreprise": "legal_form",
    "forme juridique": "legal_form",
    "legal form": "legal_form",
    "adresse": "address",
    "address": "address",
    "adresse complete": "address",
    "rue": "address.street",
    "street": "address.street",
    "street address": "address.street",
    "ville": "address.city",
    "city": "address.city",
    "code postal": "address.postal_code",
    "postal code": "address.postal_code",
    "zip": "address.postal_code",
    "adresse du siege": "headquarters_address",
    "siege social": "headquarters_address",
    "capital social": "share_capital",
    "share capital": "share_capital",
    # activité (audit catalogue — re-verdicts Teamleader + standards CRM)
    "secteur": "industry",
    "secteur d activite": "industry",
    "industry": "industry",
    "industrie": "industry",
    "effectif": "employee_count",
    "effectifs": "employee_count",
    "nombre d employes": "employee_count",
    "nombre de salaries": "employee_count",
    "number of employees": "employee_count",
    "employee count": "employee_count",
    "employees": "employee_count",
    "headcount": "employee_count",
    "collaborateurs": "employee_count",  # « # Collaborateurs » normalisé
    "code ape": "activity_code",
    "code naf": "activity_code",
    "nace": "activity_code",
    "code nace": "activity_code",
    "activity code": "activity_code",
    # immatriculation : la date de création d'une société = son
    # immatriculation (verdict audit — alias, pas de second preset)
    "date de creation": "registration_date",
    "date de constitution": "registration_date",
    "date d immatriculation": "registration_date",
    "incorporation date": "registration_date",
    "registration date": "registration_date",
    "tags": "tags",
    "etiquettes": "tags",
    "label": "tags",
    "labels": "tags",
}

COMPANY_FALLBACK_ALIASES: Final[dict[str, str]] = {
    "mobile": "phone",
    "cell": "phone",
    "portable": "phone",
    # domaine : REPLI — l'URL pleine (« Website URL ») gagne si présente ;
    # le domaine HubSpot (leur clé de dédup) ne sert que faute de mieux.
    "domain": "website",
    "company domain name": "website",
    "domaine": "website",
    "nom de domaine": "website",
}


def suggest_mapping(
    headers: list[str],
    valid_targets: set[str],
    *,
    aliases: dict[str, str],
    fallback_aliases: Mapping[str, str | tuple[str, ...]],
    excluded: frozenset[str],
    extra_keys: dict[str, str] | None = None,
    ambiguous: dict[str, list[str]] | None = None,
    street_pair_target: str | None = None,
) -> tuple[dict[str, str], dict[str, list[str]], list[str]]:
    """(suggestions {colonne: cible}, colonnes sans suggestion). Une cible
    n'est prise qu'une fois — la première colonne gagne, ordre du fichier.
    SEULE exception : le couple rue + numéro (`street_pair_target`), deux
    colonnes convergentes vers le même street — le numéro n'est suggéré
    que si une colonne rue a pris la cible, jamais seul.
    `extra_keys` : alias dynamiques (clé normalisée → cible), typiquement
    les clés/labels des défs d'agence.
    Un repli peut être une CHAÎNE de cibles (tuple) : on descend les crans
    jusqu'au premier libre — Mobile→phone, puis →secondary_phone."""
    suggestions: dict[str, str] = {}
    offered: dict[str, list[str]] = {}
    unmatched: list[str] = []
    taken: set[str] = set()
    dynamic = extra_keys or {}
    ambiguous_map = ambiguous or {}
    fallback_pending: list[tuple[str, tuple[str, ...]]] = []
    pair_pending: list[str] = []

    for header in headers:
        n = normalize_header(header)
        if not n or n in excluded:
            unmatched.append(header)
            continue
        if street_pair_target and n in STREET_NUMBER_HEADERS:
            pair_pending.append(header)
            continue
        if n in ambiguous_map:
            options = [t for t in ambiguous_map[n] if t in valid_targets and t not in taken]
            if options:
                offered[header] = options
            else:
                unmatched.append(header)
            continue
        target = aliases.get(n) or dynamic.get(n)
        if target is None and n in fallback_aliases:
            chain = fallback_aliases[n]
            fallback_pending.append((header, (chain,) if isinstance(chain, str) else chain))
            continue
        if target is None:
            # FUZZY prudent, dernier recours : proche d'un alias connu.
            close = difflib.get_close_matches(n, list(aliases) + list(dynamic), n=1, cutoff=0.85)
            if close:
                target = aliases.get(close[0]) or dynamic.get(close[0])
        if target and target in valid_targets and target not in taken:
            suggestions[header] = target
            taken.add(target)
        else:
            unmatched.append(header)

    # Replis : seulement si la cible est restée libre (Mobile→phone), et
    # cran par cran quand le repli est une chaîne (→secondary_phone).
    for header, chain in fallback_pending:
        for target in chain:
            if target in valid_targets and target not in taken:
                suggestions[header] = target
                taken.add(target)
                break
        else:
            unmatched.append(header)
    # Le couple : le numéro rejoint le street déjà pris par la rue.
    for header in pair_pending:
        if street_pair_target in taken:
            suggestions[header] = str(street_pair_target)
        else:
            unmatched.append(header)

    # LES DEUX MODES D'ADRESSE NE COHABITENT PAS — et c'est LE SUGGÉREUR
    # qui tranche. L'import refuse une base mappée à la fois en texte
    # intégral et par sous-champs (422 `import.address_mode_conflict`) ;
    # proposer les deux, c'est proposer une charge qui ne passe pas — le
    # cas EXACT des 42 en-têtes Teamleader (Rue/CP/Ville + « adresse
    # postale »), où tout import de la suggestion brute échouait.
    # LES MORCEAUX GAGNENT (plus fins que le texte collé) : la colonne
    # texte repart en `unmatched`, sa cible redevient LIBRE — l'agence
    # peut basculer sur le mode intégral à la main si elle préfère.
    # Post-passe, donc indépendante de l'ordre des colonnes du fichier.
    composed_bases = {t.split(".", 1)[0] for t in suggestions.values() if "." in t}
    for header in [h for h, t in suggestions.items() if t in composed_bases]:
        taken.discard(suggestions.pop(header))
        unmatched.append(header)
    # Même règle sur les cibles PROPOSÉES au choix (l'ambiguïté « Pays ») :
    # une option qui heurterait le mode déjà suggéré n'est pas offerte —
    # sinon l'agence assemble elle-même le 422 depuis notre propre menu.
    address_bases = {t.split(".", 1)[0] for t in valid_targets if "." in t}
    text_bases = {t for t in suggestions.values() if t in address_bases}
    for header in list(offered):
        options = [
            t
            for t in offered[header]
            if not ("." in t and t.split(".", 1)[0] in text_bases) and t not in composed_bases
        ]
        if options:
            offered[header] = options
        else:
            del offered[header]
            unmatched.append(header)
    return suggestions, offered, unmatched

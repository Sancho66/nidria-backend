"""LE VOCABULAIRE ×7 DES CIBLES D'IMPORT que le catalogue ne porte pas.

`GET /imports/targets` sert chaque cible avec son `label_i18n` — la
mécanique du fix traductions appliquée aux cibles : le back livre les 7
langues, le visiteur lit la sienne, et le front n'a plus de table de
libellés à tenir.

Le catalogue (`FIELD_PRESETS`) couvre 88 clés, mais l'univers d'import
déborde : l'identité (le trio du contrat person), les 10 colonnes
CIVILES natives (des colonnes, pas des définitions — elles n'ont jamais
eu de preset), les cibles structurelles (`tags`, `preferred_lang`), les
4 sous-champs d'adresse, et 7 clés du plan de valeurs SOCIÉTÉ absentes
du catalogue back. Elles vivent ici, dans les mêmes 7 langues.

Les libellés sont ceux que le front affichait DÉJÀ (`steps:
requirementBaseField`, `imports:identity`, `imports:mapping.address`,
`catalog:fields` pour les 7 clés société) : déménagés, pas réécrits —
l'écran ne change pas de mots, il change de source.

Pourquoi pas dans `FIELD_PRESETS` : un preset du catalogue est
DÉCLARABLE (le picker de collecte l'offre, le seed l'instancie). Une
colonne civile ne se déclare pas, `tags` n'est pas un champ, et les 7
clés société n'ont pas de face personne. Les mettre au catalogue
changerait le produit ; ici, elles ne font que se dire.
"""

from typing import Final

# Le trio du contrat person — les 3 colonnes obligatoires de l'import.
IDENTITY_LABELS: Final[dict[str, dict[str, str]]] = {
    "first_name": {
        "fr": "Prénom",
        "en": "First name",
        "es": "Nombre",
        "ru": "Имя",
        "pt": "Nome próprio",
        "it": "Nome",
        "hu": "Keresztnév",
    },
    "last_name": {
        "fr": "Nom",
        "en": "Last name",
        "es": "Apellido",
        "ru": "Фамилия",
        "pt": "Apelido",
        "it": "Cognome",
        "hu": "Vezetéknév",
    },
    "email": {
        "fr": "Email",
        "en": "Email",
        "es": "Correo",
        "ru": "Email",
        "pt": "Email",
        "it": "Email",
        "hu": "E-mail",
    },
}

# Les 10 colonnes civiles natives + la colonne LANGUE (`preferred_lang`,
# seule vérité depuis la mort du preset preferred_language).
CIVIL_LABELS: Final[dict[str, dict[str, str]]] = {
    "passport_number": {
        "fr": "Passeport",
        "en": "Passport",
        "es": "Pasaporte",
        "ru": "Паспорт",
        "pt": "Passaporte",
        "it": "Passaporto",
        "hu": "Útlevél",
    },
    "date_of_birth": {
        "fr": "Date de naissance",
        "en": "Date of birth",
        "es": "Fecha de nacimiento",
        "ru": "Дата рождения",
        "pt": "Data de nascimento",
        "it": "Data di nascita",
        "hu": "Születési dátum",
    },
    "nationality": {
        "fr": "Nationalité",
        "en": "Nationality",
        "es": "Nacionalidad",
        "ru": "Гражданство",
        "pt": "Nacionalidade",
        "it": "Nazionalità",
        "hu": "Állampolgárság",
    },
    "place_of_birth": {
        "fr": "Lieu de naissance",
        "en": "Place of birth",
        "es": "Lugar de nacimiento",
        "ru": "Место рождения",
        "pt": "Local de nascimento",
        "it": "Luogo di nascita",
        "hu": "Születési hely",
    },
    "sex": {
        "fr": "Sexe",
        "en": "Sex",
        "es": "Sexo",
        "ru": "Пол",
        "pt": "Sexo",
        "it": "Sesso",
        "hu": "Nem",
    },
    "marital_status": {
        "fr": "Situation familiale",
        "en": "Marital status",
        "es": "Estado civil",
        "ru": "Семейное положение",
        "pt": "Estado civil",
        "it": "Stato civile",
        "hu": "Családi állapot",
    },
    "phone": {
        "fr": "Téléphone",
        "en": "Phone",
        "es": "Teléfono",
        "ru": "Телефон",
        "pt": "Telefone",
        "it": "Telefono",
        "hu": "Telefon",
    },
    "birth_name": {
        "fr": "Nom de naissance",
        "en": "Birth name",
        "es": "Nombre de nacimiento",
        "ru": "Фамилия при рождении",
        "pt": "Nome de nascimento",
        "it": "Cognome di nascita",
        "hu": "Születési név",
    },
    "profession": {
        "fr": "Profession",
        "en": "Occupation",
        "es": "Profesión",
        "ru": "Профессия",
        "pt": "Profissão",
        "it": "Professione",
        "hu": "Foglalkozás",
    },
    "employer": {
        "fr": "Employeur",
        "en": "Employer",
        "es": "Empleador",
        "ru": "Работодатель",
        "pt": "Entidade empregadora",
        "it": "Datore di lavoro",
        "hu": "Munkáltató",
    },
    "preferred_lang": {
        "fr": "Langue de contact",
        "en": "Contact language",
        "es": "Idioma de contacto",
        "ru": "Язык общения",
        "pt": "Idioma de contacto",
        "it": "Lingua di contatto",
        "hu": "Kapcsolattartás nyelve",
    },
}

# Le type de donnée des colonnes civiles (le catalogue n'en dit rien —
# ce sont des colonnes). Sert la reconnaissance de format côté grille.
CIVIL_FIELD_TYPES: Final[dict[str, str]] = {
    "passport_number": "text",
    "date_of_birth": "date",
    "nationality": "country",
    "place_of_birth": "text",
    "sex": "select",
    "marital_status": "select",
    "phone": "text",
    "birth_name": "text",
    "profession": "text",
    "employer": "text",
    "preferred_lang": "select",
}

# `tags` : ni civil ni preset — une colonne d'étiquettes se déverse dans
# les tags de la fiche (split ,/; dédupliqué), des deux côtés.
TAGS_LABEL: Final[dict[str, str]] = {
    "fr": "Étiquettes (tags)",
    "en": "Tags",
    "es": "Etiquetas (tags)",
    "ru": "Метки (теги)",
    "pt": "Etiquetas (tags)",
    "it": "Etichette (tag)",
    "hu": "Címkék",
}

# Les 4 morceaux d'une adresse, dans l'ordre de lecture d'une enveloppe.
ADDRESS_SUBFIELD_LABELS: Final[dict[str, dict[str, str]]] = {
    "street": {
        "fr": "Rue",
        "en": "Street",
        "es": "Calle",
        "ru": "Улица",
        "pt": "Rua",
        "it": "Via",
        "hu": "Utca",
    },
    "postal_code": {
        "fr": "Code postal",
        "en": "Postal code",
        "es": "Código postal",
        "ru": "Почтовый индекс",
        "pt": "Código postal",
        "it": "CAP",
        "hu": "Irányítószám",
    },
    "city": {
        "fr": "Ville",
        "en": "City",
        "es": "Ciudad",
        "ru": "Город",
        "pt": "Cidade",
        "it": "Città",
        "hu": "Város",
    },
    "country": {
        "fr": "Pays",
        "en": "Country",
        "es": "País",
        "ru": "Страна",
        "pt": "País",
        "it": "Paese",
        "hu": "Ország",
    },
}

# La DÉNOMINATION société : la clé de dédup de l'import sociétés (sans
# elle, le back refuse — `import.name_target_required`).
COMPANY_NAME_LABEL: Final[dict[str, str]] = {
    "fr": "Nom de la société",
    "en": "Company name",
    "es": "Nombre de la empresa",
    "ru": "Название компании",
    "pt": "Nome da empresa",
    "it": "Nome della società",
    "hu": "Cég neve",
}

# Les 7 clés du plan de valeurs SOCIÉTÉ que le catalogue back n'a pas
# (elles n'ont pas de face personne : le catalogue sert la collecte
# dossier, ces clés ne servent que la fiche société et son import).
COMPANY_EXTRA_LABELS: Final[dict[str, dict[str, str]]] = {
    "address": {
        "fr": "Adresse",
        "en": "Address",
        "es": "Dirección",
        "ru": "Адрес",
        "pt": "Morada",
        "it": "Indirizzo",
        "hu": "Cím",
    },
    "vat_number": {
        "fr": "Numéro de TVA",
        "en": "VAT number",
        "es": "Número de IVA",
        "ru": "Номер НДС",
        "pt": "Número de IVA",
        "it": "Partita IVA",
        "hu": "Adószám",
    },
    "country": {
        "fr": "Pays",
        "en": "Country",
        "es": "País",
        "ru": "Страна",
        "pt": "País",
        "it": "Paese",
        "hu": "Ország",
    },
    "email": {
        "fr": "Email",
        "en": "Email",
        "es": "Email",
        "ru": "Эл. почта",
        "pt": "Email",
        "it": "Email",
        "hu": "E-mail",
    },
    "phone": {
        "fr": "Téléphone",
        "en": "Phone",
        "es": "Teléfono",
        "ru": "Телефон",
        "pt": "Telefone",
        "it": "Telefono",
        "hu": "Telefon",
    },
    "employee_count": {
        "fr": "Nombre de collaborateurs",
        "en": "Number of employees",
        "es": "Número de empleados",
        "ru": "Число сотрудников",
        "pt": "Número de colaboradores",
        "it": "Numero di dipendenti",
        "hu": "Alkalmazottak száma",
    },
    "activity_code": {
        "fr": "Code d'activité (APE / NACE)",
        "en": "Activity code (NACE / SIC)",
        "es": "Código de actividad (CNAE)",
        "ru": "Код деятельности (ОКВЭД)",
        "pt": "Código de atividade (CAE)",
        "it": "Codice attività (ATECO)",
        "hu": "Tevékenységi kód (TEÁOR)",
    },
}

# Le type des 7 ci-dessus (les autres presets société tirent le leur du
# catalogue). `address` est composable, les compteurs sont numériques.
COMPANY_EXTRA_FIELD_TYPES: Final[dict[str, str]] = {
    "address": "address",
    "vat_number": "text",
    "country": "country",
    "email": "text",
    "phone": "text",
    "employee_count": "number",
    "activity_code": "text",
}

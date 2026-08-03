"""Normalisation des VALEURS d'import (demande urgente 04/08).

LA RÈGLE STRUCTURELLE : toute cible que suggest-mapping propose DOIT
avoir une coercition qui accepte les formats du monde réel de sa colonne
(gravée en test — un suggéreur qui propose une cible incoerçable casse).

Tables EXPLICITES en code (pas de lib) :
- langue : codes ISO-639-1 + noms complets dans les langues du produit,
  normalisés vers l'option CANONIQUE du select de la déf (les options
  sont localisées par agence — on passe par une identité de langue) ;
- sexe : M/F/X + Homme/Femme/Male/Female/H… ;
- état civil : les libellés FR/EN courants vers l'enum.
Échec de normalisation = valeur inchangée — la coercition aval décide
(trou + issue, jamais un 500 : la règle absolue tient)."""

import unicodedata
from typing import Final


def _norm(value: str) -> str:
    s = unicodedata.normalize("NFKD", value)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return " ".join(s.lower().split())


# Identité de langue → toutes ses écritures connues (ISO + noms dans les
# langues du produit + variantes courantes).
_LANGUAGE_FORMS: Final[dict[str, frozenset[str]]] = {
    "fr": frozenset(
        {
            "fr",
            "fra",
            "fre",
            "francais",
            "french",
            "frances",
            "franzosisch",
            "francese",
            "французский",
            "francia",
        }
    ),
    "en": frozenset(
        {"en", "eng", "anglais", "english", "ingles", "englisch", "inglese", "английский", "angol"}
    ),
    "es": frozenset(
        {
            "es",
            "spa",
            "espagnol",
            "spanish",
            "espanol",
            "spanisch",
            "spagnolo",
            "испанский",
            "spanyol",
        }
    ),
    "pt": frozenset(
        {
            "pt",
            "por",
            "portugais",
            "portuguese",
            "portugues",
            "portugiesisch",
            "portoghese",
            "португальский",
            "portugal",
        }
    ),
    "ru": frozenset(
        {"ru", "rus", "russe", "russian", "ruso", "russo", "russisch", "русский", "orosz"}
    ),
    "de": frozenset(
        {
            "de",
            "deu",
            "ger",
            "allemand",
            "german",
            "aleman",
            "deutsch",
            "tedesco",
            "немецкий",
            "nemet",
        }
    ),
    "it": frozenset(
        {"it", "ita", "italien", "italian", "italiano", "italienisch", "итальянский", "olasz"}
    ),
    "other": frozenset({"other", "autre", "otro", "outro", "altro", "andere", "другой", "egyeb"}),
}


def normalize_language_value(raw: str, options: list[str] | None) -> str:
    """'FR' / 'Français' / 'French' / 'francés'… → l'option canonique du
    SELECT de la déf (quelle que soit sa langue de déclaration). Inconnu →
    valeur inchangée (le select aval tranchera : trou + issue)."""
    n = _norm(raw)
    identity = next((k for k, forms in _LANGUAGE_FORMS.items() if n in forms), None)
    if identity is None or not options:
        return raw
    for option in options:
        if _norm(option) in _LANGUAGE_FORMS[identity]:
            return option
    return raw


_SEX_FORMS: Final[dict[str, str]] = {
    "m": "M",
    "male": "M",
    "homme": "M",
    "h": "M",
    "masculin": "M",
    "mr": "M",
    "monsieur": "M",
    "f": "F",
    "female": "F",
    "femme": "F",
    "feminin": "F",
    "mme": "F",
    "madame": "F",
    "w": "F",
    "x": "X",
    "other": "X",
    "autre": "X",
    "non binaire": "X",
    "nonbinary": "X",
    "divers": "X",
}


def normalize_sex_value(raw: str) -> str:
    return _SEX_FORMS.get(_norm(raw), raw)


_MARITAL_FORMS: Final[dict[str, str]] = {
    "celibataire": "single",
    "single": "single",
    "soltero": "single",
    "soltera": "single",
    "marie": "married",
    "mariee": "married",
    "married": "married",
    "casado": "married",
    "casada": "married",
    "divorce": "divorced",
    "divorcee": "divorced",
    "divorced": "divorced",
    "divorciado": "divorced",
    "veuf": "widowed",
    "veuve": "widowed",
    "widowed": "widowed",
    "viudo": "widowed",
    "viuda": "widowed",
    "pacse": "partnership",
    "pacsee": "partnership",
    "pacs": "partnership",
    "union libre": "partnership",
    "concubinage": "partnership",
    "partnership": "partnership",
    "civil partnership": "partnership",
}


def normalize_marital_value(raw: str) -> str:
    return _MARITAL_FORMS.get(_norm(raw), raw)


def normalize_import_value(target: str, raw: str, options: list[str] | None = None) -> str:
    """Le point d'entrée UNIQUE de l'import : la valeur brute d'une cible
    connue passe sa table ; cible sans table → valeur inchangée."""
    if target == "sex":
        return normalize_sex_value(raw)
    if target == "marital_status":
        return normalize_marital_value(raw)
    if target == "preferred_language":
        return normalize_language_value(raw, options)
    return raw


def normalize_number_value(raw: str) -> str:
    """Formats numériques du monde réel → forme coerçable : symboles
    monétaires retirés, séparateurs de milliers (espace, insécable, fine)
    ôtés ENTRE chiffres, virgule décimale unique → point. Une plage
    (« 51-200 ») reste telle quelle — la coercition aval la refuse
    (issue + trou, jamais un 500)."""
    s = raw.strip()
    for symbol in ("€", "$", "£"):
        s = s.replace(symbol, "")
    s = s.strip()
    separators = (" ", " ", " ")
    s = "".join(
        c
        for i, c in enumerate(s)
        if not (
            c in separators and 0 < i < len(s) - 1 and s[i - 1].isdigit() and s[i + 1].isdigit()
        )
    )
    if s.count(",") == 1 and "." not in s:
        s = s.replace(",", ".")
    return s


# --- composition d'adresse (lot composition visible) ----------------------------------

ADDRESS_SUBFIELDS: Final[tuple[str, ...]] = ("street", "city", "postal_code", "country")
_ADDRESS_CAPS: Final[dict[str, int]] = {"street": 255, "city": 100, "postal_code": 20}


def assemble_address(parts: dict[str, str]) -> tuple[dict[str, str], list[str]]:
    """Sous-champs mappés → l'objet adresse PROPRE. Validation par
    sous-champ (caps de la règle V1, pays ISO-2) : un sous-champ mauvais
    = retiré + signalé, le reste s'assemble — la règle absolue à la
    granularité du sous-champ. Retourne (objet, sous-champs en échec)."""
    from src.custom_fields.custom_fields_validation import _coerce_country

    out: dict[str, str] = {}
    failed: list[str] = []
    for sub, raw in parts.items():
        value = raw.strip()
        if not value:
            continue
        if sub == "country":
            try:
                out["country"] = _coerce_country(value)
            except ValueError:
                failed.append(sub)
            continue
        cap = _ADDRESS_CAPS.get(sub)
        if cap is not None and len(value) > cap:
            failed.append(sub)
            continue
        out[sub] = value
    return out, failed

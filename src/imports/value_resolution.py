"""Lossless import boundaries: resolve known labels, retain unknown source text."""

import json
import re
from functools import cache
from typing import Any

import pycountry

from shared.models.custom_field import CustomFieldDefinition
from src.core.countries import _catalog, country_name
from src.core.i18n import SUPPORTED_LANGUAGES
from src.custom_fields.custom_fields_validation import _coerce_one
from src.imports.value_normalizers import _norm


@cache
def country_labels() -> dict[str, str]:
    candidates: dict[str, set[str]] = {}
    for country in pycountry.countries:
        labels = {country.alpha_2, country.alpha_3, country.name}
        for language in SUPPORTED_LANGUAGES:
            labels.add(country_name(country.alpha_2, language) or country.name)
            for attribute in ("common_name", "official_name"):
                label = getattr(country, attribute, None)
                if label:
                    labels.add(_catalog(language).gettext(label))
        for label in labels:
            candidates.setdefault(_norm(label), set()).add(country.alpha_2)
    return {label: next(iter(codes)) for label, codes in candidates.items() if len(codes) == 1}


def country_value(raw: str) -> str:
    # Never select one member of a compound country value or infer a
    # country from a city/region. Unresolved text goes back to the operator.
    return country_labels().get(_norm(raw), raw)


def option_value(raw: str, options: list[str]) -> str:
    if raw in options:
        return raw
    matches = [option for option in options if _norm(option) == _norm(raw)]
    return matches[0] if len(matches) == 1 else raw


def coerce_import_field(definition: CustomFieldDefinition, raw: str) -> Any:
    value: Any = raw
    if definition.field_type == "country":
        value = country_value(raw)
    elif definition.field_type in ("select", "multi_select"):
        options = definition.option_values
        if definition.field_type == "select":
            value = option_value(raw, options)
        else:
            # Prefer an entire option: punctuation may belong to its label.
            entire = option_value(raw, options)
            if entire in options:
                value = [entire]
            else:
                try:
                    decoded = json.loads(raw)
                except ValueError:
                    decoded = None
                parts = decoded if isinstance(decoded, list) else re.split(r"[;,]", raw)
                if not all(isinstance(part, str) for part in parts):
                    raise ValueError("expects a list of option labels")
                value = list(dict.fromkeys(option_value(part.strip(), options) for part in parts))
    return _coerce_one(definition, value)

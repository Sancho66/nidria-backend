"""Validate/coerce submitted custom-field values against the agency's
active definitions. Shared by the case_person create/update paths.

Semantics (DÉGEL 2, point 1): the PATCH is a partial MERGE keyed by
`key`. Only the keys PRESENT in the payload are validated and applied —
keys absent are left untouched. This is what makes editing a person on
some other field possible even after a `required` field was added: the
required is enforced only when its key is explicitly present-but-empty,
never retroactively on a key the patch doesn't mention.
"""

import re
from collections.abc import Iterable
from datetime import date, datetime
from typing import Any

from shared.models.custom_field import CustomFieldDefinition
from src.core.enums import CustomFieldType
from src.core.exceptions import ValidationError

_DATETIME_FORMATS = ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S")
# ISO 3166-1 alpha-2 — the SAME rule as CaseUpdateRequest.origin_country,
# so a custom country field validates identically to the canonical columns.
_COUNTRY_RE = re.compile(r"^[A-Z]{2}$")


def _coerce_country(value: Any) -> str:
    s = str(value).strip()
    if not _COUNTRY_RE.match(s):
        raise ValueError("expects a 2-letter ISO country code (e.g. FR)")
    return s


def _is_empty(value: Any) -> bool:
    if value is None or value == "" or value == []:
        return True
    # An ADDRESS sub-object with no non-empty sub-field reads as empty
    # ({} or {street:null, city:null, ...}) → cleared / pending. Other
    # types never store dicts, so this is safe and generic.
    if isinstance(value, dict):
        return all(_is_empty(v) for v in value.values())
    return False


# The ordered address sub-fields — the SINGLE source of truth for "what an
# address is made of", reused by the composite CSV mapping (N columns → one
# address object). `country` reuses _coerce_country (ISO-2), the string
# sub-fields are length-capped; NO rule is duplicated.
ADDRESS_SUBFIELDS: tuple[str, ...] = ("street", "city", "postal_code", "country")
_ADDRESS_MAXLEN: dict[str, int] = {"street": 255, "city": 100, "postal_code": 20}


def coerce_address_subfield(subfield: str, value: Any) -> str:
    """Validate/coerce ONE non-empty address sub-field. `country` REUSES
    _coerce_country (the same ISO-2 rule as the country type and the canonical
    columns); the string sub-fields are trimmed and length-capped. This is the
    primitive both `_coerce_address` (whole object) and the composite CSV
    import (one column → one sub-field) build on."""
    if subfield == "country":
        return _coerce_country(value)  # ← V1 rule, single source
    maxlen = _ADDRESS_MAXLEN.get(subfield)
    if maxlen is None:
        raise ValueError(f"unknown address sub-field {subfield!r}")
    s = str(value).strip()
    if len(s) > maxlen:
        raise ValueError(f"{subfield} too long (max {maxlen} characters)")
    return s


def _coerce_address(value: Any) -> dict[str, str]:
    """Validate a structured address sub-object. All sub-fields are OPTIONAL
    (a partial {city, country} is valid): each present, non-empty one is
    coerced via `coerce_address_subfield`; empty/missing sub-fields are
    dropped (absent, not forced-empty); unknown keys are ignored."""
    if not isinstance(value, dict):
        raise ValueError("expects an address object {street, city, postal_code, country}")
    out: dict[str, str] = {}
    for sub in ADDRESS_SUBFIELDS:
        raw = value.get(sub)
        if raw is None or str(raw).strip() == "":
            continue
        out[sub] = coerce_address_subfield(sub, raw)
    return out


def _coerce_number(value: Any) -> float | int:
    if isinstance(value, bool):  # bool is an int subclass — reject explicitly
        raise ValueError("expects a number")
    if isinstance(value, int | float):
        return value
    try:
        text = str(value).strip()
        return int(text) if text.lstrip("-").isdigit() else float(text)
    except (TypeError, ValueError) as exc:
        raise ValueError("expects a number") from exc


def _coerce_date(value: Any) -> str:
    if isinstance(value, datetime | date):
        return value.isoformat()
    s = str(value).strip()
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        pass
    for fmt in _DATETIME_FORMATS:
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    raise ValueError("expects a date (YYYY-MM-DD)")


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ("true", "false", "1", "0"):
        return value.strip().lower() in ("true", "1")
    raise ValueError("expects a boolean")


def _coerce_one(definition: CustomFieldDefinition, value: Any) -> Any:
    """Coerce/validate a non-empty value against the field type. Raises
    ValueError with a human message (caller prefixes the field label)."""
    ftype = definition.field_type
    if ftype == CustomFieldType.TEXT.value:
        return str(value)
    if ftype == CustomFieldType.NUMBER.value:
        return _coerce_number(value)
    if ftype == CustomFieldType.DATE.value:
        return _coerce_date(value)
    if ftype == CustomFieldType.BOOLEAN.value:
        return _coerce_bool(value)
    if ftype == CustomFieldType.COUNTRY.value:
        return _coerce_country(value)
    if ftype == CustomFieldType.ADDRESS.value:
        return _coerce_address(value)
    options = set(definition.option_values)
    if ftype == CustomFieldType.SELECT.value:
        if value not in options:
            raise ValueError(f"must be one of {sorted(options)}")
        return value
    if ftype == CustomFieldType.MULTI_SELECT.value:
        if not isinstance(value, list):
            raise ValueError("expects a list of values")
        invalid = [v for v in value if v not in options]
        if invalid:
            raise ValueError(f"contains values outside {sorted(options)}: {invalid}")
        return value
    raise ValueError(f"unknown field type {ftype!r}")


def validate_and_merge(
    active_definitions: Iterable[CustomFieldDefinition],
    current: dict[str, Any],
    submitted: dict[str, Any],
) -> dict[str, Any]:
    """Return the merged custom_fields after validating `submitted`.

    - Unknown / archived key in the payload → 422 (strict).
    - Each submitted value coerced/validated by its type; errors are
      ACCUMULATED (all bad fields reported, not the first).
    - required + present-but-empty → 422 (the bounded rule of point 1).
    - A null/empty value on a non-required field clears the key.
    """
    by_key = {d.key: d for d in active_definitions}
    errors: list[str] = []
    merged = dict(current)

    for key, value in submitted.items():
        definition = by_key.get(key)
        if definition is None:
            errors.append(f"unknown or archived custom field: {key!r}")
            continue
        label = f"'{definition.label}' ({key})"
        if _is_empty(value):
            if definition.required:
                errors.append(f"Field {label}: required, cannot be empty.")
            else:
                merged.pop(key, None)  # clear
            continue
        try:
            merged[key] = _coerce_one(definition, value)
        except ValueError as exc:
            errors.append(f"Field {label}: {exc}.")

    if errors:
        raise ValidationError("; ".join(errors))
    return merged


def visible_values(
    active_definitions: Iterable[CustomFieldDefinition], stored: dict[str, Any]
) -> dict[str, Any]:
    """Read projection: only keys with an ACTIVE definition are exposed.
    Orphan keys (definition archived/deleted after the value was saved)
    stay in the DB but are not surfaced."""
    active_keys = {d.key for d in active_definitions}
    return {k: v for k, v in stored.items() if k in active_keys}

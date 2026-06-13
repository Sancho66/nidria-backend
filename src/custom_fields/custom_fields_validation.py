"""Validate/coerce submitted custom-field values against the agency's
active definitions. Shared by the case_person create/update paths.

Semantics (DÉGEL 2, point 1): the PATCH is a partial MERGE keyed by
`key`. Only the keys PRESENT in the payload are validated and applied —
keys absent are left untouched. This is what makes editing a person on
some other field possible even after a `required` field was added: the
required is enforced only when its key is explicitly present-but-empty,
never retroactively on a key the patch doesn't mention.
"""

from collections.abc import Iterable
from datetime import date, datetime
from typing import Any

from shared.models.custom_field import CustomFieldDefinition
from src.core.enums import CustomFieldType
from src.core.exceptions import ValidationError

_DATETIME_FORMATS = ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S")


def _is_empty(value: Any) -> bool:
    return value is None or value == "" or value == []


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

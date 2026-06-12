"""Translate an `AdvancedFilters` tree into SQLAlchemy clauses —
ported from Prism (companies/filter_builder), adapted Company →
ClientCase.

Field families:

- First-class ClientCase columns (FIELD_MAP), datetime values coerced
  before binding (`between` on a varchar vs timestamp raises in PG).
- `principal_*`: filters on the case's principal expat (first_name,
  last_name, email, preferred_lang) via an IN-subquery so the listing
  stays single-row-per-case.
- `tags`: the JSONB label list on ClientCase (contains / not_contains
  = ANY of the given labels, is_empty / is_not_empty on list length).

Everything else raises ValidationError so the listing endpoint
returns a clean 422 instead of a 500.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime
from typing import Any

from sqlalchemy import String, and_, cast, func, or_, select
from sqlalchemy.sql.elements import ColumnElement

from shared.models.client_case import ClientCase
from shared.models.expat_user import ExpatUser
from src.cases.filter_schema import AdvancedFilters, FilterCondition, FilterGroup
from src.core.exceptions import ValidationError

# `dict[str, Any]`: values are InstrumentedAttribute descriptors, not
# ColumnElement subclasses in the stubs (same note as Prism).
FIELD_MAP: dict[str, Any] = {
    "status": ClientCase.status,
    "source": ClientCase.source,
    "origin_country": ClientCase.origin_country,
    "dest_country": ClientCase.dest_country,
    "owner_agent_id": ClientCase.owner_agent_id,
    "journey_template_id": ClientCase.journey_template_id,
    "created_at": ClientCase.created_at,
    "updated_at": ClientCase.updated_at,
}

_PRINCIPAL_FIELDS: dict[str, Any] = {
    "first_name": ExpatUser.first_name,
    "last_name": ExpatUser.last_name,
    "email": ExpatUser.email,
    "preferred_lang": ExpatUser.preferred_lang,
}


# ISO 8601 variants the frontend / saved views actually emit. Anything
# beyond these is a 422 — strict, so a typo isn't silently coerced.
_DATETIME_FORMATS: tuple[str, ...] = (
    "%Y-%m-%d",  # date picker default ("2026-05-12")
    "%Y-%m-%dT%H:%M:%S",  # `<input type="datetime-local">`
    "%Y-%m-%dT%H:%M:%S.%f",  # JSON.stringify(new Date()) before "Z"
    "%Y-%m-%dT%H:%M:%SZ",  # ISO with explicit UTC marker
    "%Y-%m-%dT%H:%M:%S.%fZ",  # JSON.stringify(new Date()) — full form
)


def _to_datetime(value: Any) -> datetime:
    """Parse the JSON-decoded value into a `datetime`. Strict on miss —
    a typo must surface as 422 rather than silently become `now()`."""
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, datetime.min.time())
    s = str(value).strip()
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        pass
    for fmt in _DATETIME_FORMATS:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    raise ValueError(f"Cannot parse datetime: {value!r}")


_VALUE_CASTERS: dict[str, Callable[[Any], Any]] = {
    "created_at": _to_datetime,
    "updated_at": _to_datetime,
}

# Operators that ignore their value entirely — skip the cast so
# `_to_datetime(None)` can't blow up on a value-less is_empty.
_VALUE_FREE_OPERATORS: frozenset[str] = frozenset({"is_empty", "is_not_empty"})


def _cast_value(field: str, value: Any, op: str) -> Any:
    if op in _VALUE_FREE_OPERATORS or value is None:
        return None
    caster = _VALUE_CASTERS.get(field)
    if caster is None:
        return value
    try:
        if isinstance(value, list):
            return [caster(v) for v in value if v is not None]
        return caster(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(
            f"Cannot coerce filter value for {field!r} ({value!r}): {exc}"
        ) from exc


def _apply_operator(column: Any, op: str, value: Any) -> Any:
    """Apply a single operator to a SQLAlchemy column expression.
    Ported verbatim from Prism."""
    if op == "eq":
        return column == value
    if op == "neq":
        return column != value
    if op == "gt":
        return column > value
    if op == "gte":
        return column >= value
    if op == "lt":
        return column < value
    if op == "lte":
        return column <= value
    if op == "in":
        return column.in_(value if isinstance(value, list) else [value])
    if op == "not_in":
        return ~column.in_(value if isinstance(value, list) else [value])
    if op == "contains":
        return cast(column, String).ilike(f"%{value}%")
    if op == "not_contains":
        return ~cast(column, String).ilike(f"%{value}%")
    if op == "is_empty":
        return or_(column.is_(None), cast(column, String) == "")
    if op == "is_not_empty":
        return and_(column.is_not(None), cast(column, String) != "")
    if op == "between":
        if isinstance(value, list) and len(value) == 2:
            return and_(column >= value[0], column <= value[1])
        raise ValidationError("`between` requires a list of exactly 2 values")
    raise ValidationError(f"Unknown operator: {op}")


def _build_tag_clause(op: str, value: Any) -> ColumnElement[bool]:
    """ClientCase.tags is a JSONB list of labels. contains/not_contains
    match ANY of the given labels (Prism's tag semantics); emptiness is
    a length check."""
    if op in ("contains", "not_contains"):
        labels = value if isinstance(value, list) else [value]
        if not labels:
            raise ValidationError("tags filter requires at least one label")
        any_of = or_(*[ClientCase.tags.contains([label]) for label in labels])
        return any_of if op == "contains" else ~any_of
    if op in ("is_empty", "is_not_empty"):
        empty = func.jsonb_array_length(ClientCase.tags) == 0
        return empty if op == "is_empty" else ~empty
    raise ValidationError(f"Unsupported tag operator: {op}")


def _build_principal_clause(field_name: str, op: str, value: Any) -> ColumnElement[bool]:
    """Filter on the principal expat's identity fields. Wrapped in
    `principal_expat_user_id IN (<subquery>)` so the listing query
    stays single-row-per-case (same pattern as Prism's contact_*)."""
    suffix = field_name.removeprefix("principal_")
    column = _PRINCIPAL_FIELDS.get(suffix)
    if column is None:
        raise ValidationError(f"Unknown principal field: {field_name}")
    inner = _apply_operator(column, op, value)
    subquery = select(ExpatUser.id).where(inner)
    return ClientCase.principal_expat_user_id.in_(subquery)


def build_filter_clause(condition: FilterCondition) -> Any:
    """Resolve a single FilterCondition to its SQL clause: first-class
    column, principal subquery, or tags. Values are coerced to the
    column type before dispatch."""
    field, op = condition.field, condition.operator
    if field == "tags":
        return _build_tag_clause(op, condition.value)
    if field.startswith("principal_"):
        return _build_principal_clause(field, op, condition.value)
    column = FIELD_MAP.get(field)
    if column is None:
        raise ValidationError(f"Unknown filter field: {field!r}")
    value = _cast_value(field, condition.value, op)
    return _apply_operator(column, op, value)


def _build_group_clause(group: FilterGroup) -> Any:
    clauses = [build_filter_clause(c) for c in group.conditions]
    if not clauses:
        return None
    return or_(*clauses) if group.logic == "or" else and_(*clauses)


def build_advanced_clauses(filters: AdvancedFilters) -> list[Any]:
    """Flatten the tree into a list of AND-combined clauses: top-level
    conditions are ANDed, each group contributes one clause (its
    conditions combined with the group's own logic)."""
    clauses: list[Any] = [build_filter_clause(c) for c in filters.conditions]
    for group in filters.groups:
        group_clause = _build_group_clause(group)
        if group_clause is not None:
            clauses.append(group_clause)
    return clauses

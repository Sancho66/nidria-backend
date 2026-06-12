"""AdvancedFilters tree — ported from Prism (companies/filter_schema).

The frontend filter bar emits this JSON-encoded tree in the `filters`
query param of GET /cases; saved views persist it verbatim in their
`filters` JSONB."""

from typing import Any, Literal

from pydantic import BaseModel, Field

FilterOperator = Literal[
    "eq",
    "neq",
    "gt",
    "gte",
    "lt",
    "lte",
    "in",
    "not_in",
    "contains",
    "not_contains",
    "is_empty",
    "is_not_empty",
    "between",
]


class FilterCondition(BaseModel):
    """One row in the filter tree. `value` is loosely typed — the
    builder coerces per (field, operator) pair, so a `between` on
    `created_at` accepts date strings."""

    field: str
    operator: FilterOperator
    # `is_empty` / `is_not_empty` legitimately omit value; the rest
    # carry a scalar or list depending on the operator.
    value: str | int | float | bool | list[Any] | None = None


class FilterGroup(BaseModel):
    """A bag of conditions combined with the given `logic`. Groups
    themselves are AND-combined at the top level (see AdvancedFilters)."""

    logic: Literal["and", "or"] = "and"
    conditions: list[FilterCondition] = Field(default_factory=list)


class AdvancedFilters(BaseModel):
    """Top-level filter tree. `conditions` is a shortcut for the
    common "single AND group" case — equivalent to `groups=[{logic:
    "and", conditions: [...]}]`. Both fields can coexist; they're
    AND-combined in the final SQL."""

    conditions: list[FilterCondition] = Field(default_factory=list)
    groups: list[FilterGroup] = Field(default_factory=list)

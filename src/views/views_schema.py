"""Saved views — ported from Prism (src/views/views_schema)."""

import uuid
from datetime import datetime
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field

# Entities a saved view can target:
# - "cases": named table views of the cases list.
# - "cases_all": the per-agent customizable "All" view of the cases
#   list (is_default_all=True). A distinct entity value (not a flag on
#   "cases") so the (agent, agency, entity) partial unique index on
#   is_default_all rows stays per-surface — Prism's exact scheme.
Entity = Literal["cases", "cases_all"]

# The entity values a customizable "All" view can target. The
# /views/default-all endpoints and the manager reject any other.
DEFAULT_ALL_ENTITIES: Final[tuple[str, ...]] = ("cases_all",)

SortOrder = Literal["asc", "desc"]


class SavedViewRead(BaseModel):
    id: uuid.UUID
    agency_id: uuid.UUID
    agent_id: uuid.UUID
    agent_name: str
    name: str
    entity: str
    filters: dict[str, Any]
    columns: list[str] | None
    column_sizing: dict[str, int] | None
    sort_by: str | None
    sort_order: str | None
    is_default: bool
    is_default_all: bool
    is_shared: bool
    is_mine: bool
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class SavedViewCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    entity: Entity = "cases"
    filters: dict[str, Any] = Field(default_factory=dict)
    # Optional ordered list of column keys to show. None = frontend
    # defaults (see GET /cases/columns).
    columns: list[str] | None = None
    # Per-column pixel widths keyed by column slug. None = defaults.
    column_sizing: dict[str, int] | None = None
    sort_by: str | None = Field(default=None, max_length=50)
    sort_order: SortOrder | None = None
    is_shared: bool = False


class SavedViewUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    filters: dict[str, Any] | None = None
    columns: list[str] | None = None
    column_sizing: dict[str, int] | None = None
    sort_by: str | None = Field(default=None, max_length=50)
    sort_order: SortOrder | None = None
    is_shared: bool | None = None


class SavedViewDefaultAllUpdate(BaseModel):
    """Payload for PUT /views/default-all — the per-agent customizable
    "All" view. A strict subset of SavedViewUpdate by design: no
    `name` (server-controlled sentinel), no `is_shared`/`is_default`
    (an "All" view is always private and never a named default).
    `extra="forbid"` so an excluded field is a 422, not a silent drop."""

    model_config = ConfigDict(extra="forbid")

    filters: dict[str, Any] = Field(default_factory=dict)
    columns: list[str] | None = None
    column_sizing: dict[str, int] | None = None
    sort_by: str | None = Field(default=None, max_length=50)
    sort_order: SortOrder | None = None


class AvailableColumn(BaseModel):
    """One row in GET /cases/columns — a column the frontend can render.
    `default` = visible in the no-view state; `locked` = cannot be
    hidden; `type` = frontend render hint."""

    key: str
    label: str
    type: str
    default: bool = True
    locked: bool = False


class AvailableColumnsResponse(BaseModel):
    columns: list[AvailableColumn]


# The cases-list column catalog. Prism reads this from
# project_type.features (data-driven, multi-vertical); Nidria has ONE
# list → the catalog is code, same response shape.
CASE_COLUMNS: Final[tuple[AvailableColumn, ...]] = (
    AvailableColumn(key="principal", label="Client", type="text", locked=True),
    AvailableColumn(key="status", label="Statut", type="badge"),
    AvailableColumn(key="origin_country", label="Origine", type="country"),
    AvailableColumn(key="dest_country", label="Destination", type="country"),
    AvailableColumn(key="owner", label="Responsable", type="agent"),
    AvailableColumn(key="journey", label="Parcours", type="text"),
    AvailableColumn(key="tags", label="Tags", type="tags"),
    AvailableColumn(key="source", label="Source", type="text", default=False),
    AvailableColumn(key="preferred_lang", label="Langue", type="text", default=False),
    AvailableColumn(key="created_at", label="Créé le", type="datetime"),
    AvailableColumn(key="updated_at", label="Mis à jour", type="datetime", default=False),
)

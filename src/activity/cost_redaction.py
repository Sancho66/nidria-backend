"""The money in the activity journal — ONE rule for every reader.

A dossier's costs and its billed price are reserved to `cost.view`
(cases_manager._billing_block, costs_router, the export's billed columns).
The journal records the same facts: `cost.added/edited/deleted` carry the
line's amount, currency and label, and `case.updated` carries the billed
price old/new under `details.changes`. For a reader WITHOUT cost.view:

- no `cost.*` event at all;
- no `case.updated` whose ONLY changes were billing ones (an entry "the
  dossier was modified" with nothing left in it would still tell that the
  price moved);
- the other `case.updated` entries lose their billing keys.

The first two are a SQL clause — a paginated reader's `total` and page size
must not count rows it will not show. The third is a Python COPY of the
details, never the ORM row (mutating it would dirty the session and risk a
write-back of the redacted journal). A reader WITH cost.view applies
neither: nothing changes for it."""

from collections.abc import Mapping
from typing import Any

from sqlalchemy import ColumnElement, Text, and_, case, func, literal, not_, or_
from sqlalchemy.dialects.postgresql import JSONB, array

from shared.models.activity import ActivityLog

COST_ACTION_PREFIX = "cost."
CASE_UPDATED = "case.updated"
BILLING_CHANGE_KEYS: tuple[str, ...] = ("billed_amount", "billed_currency")


def cost_blind_clause() -> ColumnElement[bool]:
    """WHERE fragment for a reader WITHOUT cost.view: drops the `cost.*`
    events and the billing-only `case.updated` entries. Never NULL (a NULL
    would silently drop rows): `changes` is only inspected when it is a JSON
    object — tag edits log `case.updated` without any `changes` key."""
    changes = ActivityLog.details["changes"]
    remaining = changes
    for key in BILLING_CHANGE_KEYS:
        remaining = remaining.op("-", return_type=JSONB)(literal(key, Text))
    billing_only = case(
        (
            func.jsonb_typeof(changes) == "object",
            and_(
                changes.has_any(array(BILLING_CHANGE_KEYS, type_=Text)),
                remaining == func.jsonb_build_object(),
            ),
        ),
        else_=False,
    )
    return and_(
        not_(ActivityLog.action_type.startswith(COST_ACTION_PREFIX, autoescape=True)),
        or_(ActivityLog.action_type != CASE_UPDATED, not_(billing_only)),
    )


def hidden_without_cost(action_type: str, details: Mapping[str, Any] | None) -> bool:
    """The Python twin of `cost_blind_clause`, for a reader that already
    holds the rows and does not paginate (the case PDF): a `cost.*` event,
    or a `case.updated` whose non-empty `changes` are ALL billing keys."""
    if action_type.startswith(COST_ACTION_PREFIX):
        return True
    if action_type != CASE_UPDATED:
        return False
    changes = (details or {}).get("changes")
    return (
        isinstance(changes, Mapping)
        and bool(changes)
        and all(key in BILLING_CHANGE_KEYS for key in changes)
    )


def redact_cost_details(action_type: str, details: Mapping[str, Any] | None) -> dict[str, Any]:
    """A COPY of `details` fit for a reader WITHOUT cost.view: the billing
    keys stripped from a `case.updated`'s `changes`. The input (an ORM
    row's JSONB) is never mutated — every touched level is rebuilt."""
    redacted = dict(details or {})
    changes = redacted.get("changes")
    if action_type == CASE_UPDATED and isinstance(changes, Mapping):
        redacted["changes"] = {
            key: value for key, value in changes.items() if key not in BILLING_CHANGE_KEYS
        }
    return redacted

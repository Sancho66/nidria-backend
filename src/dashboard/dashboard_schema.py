import uuid
from datetime import date

from pydantic import BaseModel


class DashboardResponse(BaseModel):
    """Simple counts, agency-scoped. NOTHING more — rich analytics is
    V1.5 and stays there. Kept untouched (the agent-centric dashboard is
    the separate /dashboard/me); a future "agency overview" may reuse it."""

    total_cases: int
    by_status: dict[str, int]
    by_dest_country: dict[str, int]


class DashboardMeCounts(BaseModel):
    """The four personal figures for the connected agent."""

    to_realize: int  # steps I am responsible for (not done; blocked ones included)
    to_validate: int  # steps I validate, active (in_progress) → awaiting my close
    my_cases: int  # distinct active cases I am involved in (owner or step actor)
    overdue: int  # my steps whose resolved deadline is in the past


class DashboardTodoItem(BaseModel):
    """One actionable line in the unified to-do (realize + validate mixed).
    `badge` is the ROLE; `is_blocked`/`is_overdue` are state modifiers the
    front composes into the visual priority (overdue > validate > realize).
    Labels stay front (i18n) — the API ships keys/values, never FR text."""

    progress_id: uuid.UUID
    case_id: uuid.UUID
    step_name: str
    client_name: str
    dest_country: str | None
    badge: str  # "to_realize" | "to_validate"
    is_blocked: bool  # a prerequisite is not DONE → shown greyed, never hidden
    is_overdue: bool
    target_date: date | None  # resolved deadline (firm due_at or estimated-derived)


class DashboardWeeklyLoadDay(BaseModel):
    date: date
    count: int  # my actionable steps whose deadline falls on this day


class DashboardMeResponse(BaseModel):
    """Agent-centric "dashboard of action". Everything is filtered
    server-side on the connected agent (responsible OR validator == me) and
    the tenant — an agent sees ONLY its own actions."""

    first_name: str
    counts: DashboardMeCounts
    todo: list[DashboardTodoItem]  # overdue first, then by target_date
    by_status: dict[str, int]  # MY active cases by status
    weekly_load: list[DashboardWeeklyLoadDay]  # Mon→Sun of the current week

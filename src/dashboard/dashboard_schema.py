import uuid
from datetime import date, datetime

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
    count: int  # "À traiter" items landing on this day; overdue/undated/now → today


class DashboardMeResponse(BaseModel):
    """Agent-centric "dashboard of action". Everything is filtered
    server-side on the connected agent (responsible OR validator == me) and
    the tenant — an agent sees ONLY its own actions."""

    first_name: str
    counts: DashboardMeCounts
    todo: list[DashboardTodoItem]  # overdue first, then by target_date
    by_status: dict[str, int]  # MY active cases by status
    weekly_load: list[DashboardWeeklyLoadDay]  # rolling today→+6 (7 days)


class WorklistItem(BaseModel):
    """One actionable item of the unified "to handle" queue (contract
    validated 2026-07-08). `type` picks the action link the front opens:
    step_* carry progress_id (case timeline, step focused),
    document_to_review carries document_id (validation modal),
    reminder_to_approve carries reminder_id (reminders screen). A step
    both awaiting my validation AND late appears ONCE as
    step_to_validate with is_overdue=true (the action wins, the delay
    sorts); step_overdue carries the remaining late steps I must realize."""

    type: str  # step_to_validate | step_overdue | document_to_review | reminder_to_approve
    case_id: uuid.UUID
    client_name: str
    dest_country: str | None
    title: str  # step name / file name / reminder excerpt (i18n resolved)
    occurred_at: datetime  # waiting since (per-type source, v1 approximations)
    is_overdue: bool
    days_late: int | None  # None when there is no gauge
    progress_id: uuid.UUID | None = None
    document_id: uuid.UUID | None = None
    reminder_id: uuid.UUID | None = None


class WorklistResponse(BaseModel):
    """Sorted queue: overdue first (largest delay first), then oldest
    waiting first. `counts` = per-type volumes + total."""

    items: list[WorklistItem]
    counts: dict[str, int]


class ActivityItem(BaseModel):
    """One AGGREGATED client gesture: (type, case, calendar day) with
    its count - N same-day deposits by one client are one line. Types
    are the bento vocabulary: documents_uploaded | comment_added |
    account_activated | step_validated."""

    type: str
    case_id: uuid.UUID
    client_name: str  # the case principal, consistent with the worklist
    expat_user_id: uuid.UUID
    count: int
    occurred_at: datetime  # most recent of the group


class ActivityResponse(BaseModel):
    """GET /dashboard/activity - the agency-wide client pulse: 14
    sliding days, 15 aggregated items max, newest first. Demo cases
    never appear; agent gestures never appear (the agency would watch
    itself)."""

    items: list[ActivityItem]

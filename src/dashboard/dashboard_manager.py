import uuid
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from shared.models.agent import Agent
from shared.models.client_case import ClientCase
from src.core.enums import StepStatus
from src.core.i18n import DEFAULT_LANG, resolve_i18n
from src.dashboard.dashboard_repository import (
    ActivityRepository,
    DashboardRepository,
    WorklistRepository,
)
from src.dashboard.dashboard_schema import (
    ActivityItem,
    ActivityResponse,
    DashboardMeCounts,
    DashboardMeResponse,
    DashboardResponse,
    DashboardTodoItem,
    DashboardWeeklyLoadDay,
    WorklistItem,
    WorklistResponse,
)
from src.progress.progress_manager import _deadline_counter
from src.progress.progress_repository import ProgressRepository


class DashboardManager:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def _count_by(
        self,
        agency_id: uuid.UUID,
        column: InstrumentedAttribute[str] | InstrumentedAttribute[str | None],
    ) -> dict[str, int]:
        stmt = (
            select(column, func.count())
            .where(ClientCase.agency_id == agency_id, ClientCase.deleted_at.is_(None))
            .group_by(column)
        )
        return {key: count for key, count in (await self.db.execute(stmt)).all() if key is not None}

    async def get_dashboard(self, agent: Agent) -> DashboardResponse:
        by_status = await self._count_by(agent.agency_id, ClientCase.status)
        by_dest_country = await self._count_by(agent.agency_id, ClientCase.dest_country)
        return DashboardResponse(
            total_cases=sum(by_status.values()),
            by_status=by_status,
            by_dest_country=by_dest_country,
        )

    async def get_my_dashboard(self, agent: Agent, lang: str = DEFAULT_LANG) -> DashboardMeResponse:
        """Agent-centric "dashboard of action". All reads are filtered
        server-side on (agency_id, agent.id); five batched queries, no N+1:
        my open steps (join), started_ats, prerequisites, per-case done
        steps (for the BLOCKED projection), my active cases."""
        repo = DashboardRepository(self.db)
        prog = ProgressRepository(self.db)

        # i18n: the step label is resolved for `lang`; the agency default is
        # this agent's agency (all dashboard rows are agency-scoped).
        agency_default = (await repo.agency_default_language(agent.agency_id)) or DEFAULT_LANG
        rows = await repo.my_open_steps(agent.agency_id, agent.id)
        progress_ids = [r.id for r in rows]
        template_step_ids = [r.template_step_id for r in rows]
        case_ids = list({r.case_id for r in rows})

        started = await prog.started_ats(progress_ids)
        prerequisites: dict[uuid.UUID, set[uuid.UUID]] = defaultdict(set)
        for edge in await prog.list_prerequisites_for_steps(template_step_ids):
            prerequisites[edge.step_id].add(edge.prerequisite_step_id)
        done_by_case: dict[uuid.UUID, set[uuid.UUID]] = defaultdict(set)
        for case_id, step_id in await repo.done_steps_for_cases(case_ids):
            done_by_case[case_id].add(step_id)

        now = datetime.now(UTC)
        today = now.date()
        items: list[DashboardTodoItem] = []
        to_realize = to_validate = overdue = 0
        for r in rows:
            counter = _deadline_counter(r.due_at, r.estimated_days, started.get(r.id), now)
            is_overdue = counter.days_remaining is not None and counter.days_remaining < 0
            # "to validate" = I am the validator AND the step is active; else
            # I am here as responsible. A step where I am both → validate wins
            # (it is the awaiting-my-close action).
            is_validate_role = (
                r.validated_by_agent_id == agent.id and r.status == StepStatus.IN_PROGRESS.value
            )
            is_responsible = r.responsible_agent_id == agent.id
            is_blocked = r.status == StepStatus.TODO.value and any(
                p not in done_by_case[r.case_id]
                for p in prerequisites.get(r.template_step_id, set())
            )
            if is_responsible:
                to_realize += 1
            if is_validate_role:
                to_validate += 1
            if is_overdue:
                overdue += 1
            items.append(
                DashboardTodoItem(
                    progress_id=r.id,
                    case_id=r.case_id,
                    step_name=resolve_i18n(r.step_name_i18n, lang, agency_default, r.step_name),
                    client_name=f"{r.first_name} {r.last_name}".strip(),
                    dest_country=r.dest_country,
                    badge="to_validate" if is_validate_role else "to_realize",
                    is_blocked=is_blocked,
                    is_overdue=is_overdue,
                    target_date=counter.target_date.date() if counter.target_date else None,
                )
            )
        # Overdue first, then by deadline (no deadline last), then by name.
        items.sort(key=lambda i: (not i.is_overdue, i.target_date or date.max, i.step_name))

        active = await repo.my_active_cases(agent.agency_id, agent.id)
        by_status: dict[str, int] = defaultdict(int)
        for _case_id, status in active:
            by_status[status] += 1

        monday = today - timedelta(days=today.weekday())
        week = [monday + timedelta(days=offset) for offset in range(7)]
        load = dict.fromkeys(week, 0)
        for item in items:
            if item.target_date in load:
                load[item.target_date] += 1
        weekly_load = [DashboardWeeklyLoadDay(date=day, count=load[day]) for day in week]

        return DashboardMeResponse(
            first_name=agent.first_name,
            counts=DashboardMeCounts(
                to_realize=to_realize,
                to_validate=to_validate,
                my_cases=len(active),
                overdue=overdue,
            ),
            todo=items,
            by_status=dict(by_status),
            weekly_load=weekly_load,
        )


class WorklistManager(DashboardManager):
    """GET /dashboard/worklist - the unified "to handle" queue (contract
    validated 2026-07-08). Six batched queries total, no N+1: steps to
    validate (+provided_at aggregate), my open steps (overdue source),
    started_ats, documents to review, reminders to approve, agency
    default language."""

    async def get_worklist(self, agent: Agent, lang: str = DEFAULT_LANG) -> WorklistResponse:
        repo = WorklistRepository(self.db)
        prog = ProgressRepository(self.db)
        agency_default = (await repo.agency_default_language(agent.agency_id)) or DEFAULT_LANG
        now = datetime.now(UTC)
        today = now.date()

        validate_rows = await repo.steps_to_validate(agent.agency_id, agent.id)
        open_rows = await repo.my_open_steps(agent.agency_id, agent.id)
        started = await prog.started_ats(
            list({r.id for r in validate_rows} | {r.id for r in open_rows})
        )

        items: list[WorklistItem] = []

        def late_days(
            due_at: datetime | None, estimated: int | None, pid: uuid.UUID
        ) -> tuple[int | None, datetime | None]:
            counter = _deadline_counter(due_at, estimated, started.get(pid), now)
            if counter.days_remaining is not None and counter.days_remaining < 0:
                return -counter.days_remaining, counter.target_date
            return None, counter.target_date

        # 1) Steps awaiting MY validation - a late one stays ONE item
        # (the action wins, the delay sorts it first).
        validated_ids = set()
        for r in validate_rows:
            validated_ids.add(r.id)
            days, _target = late_days(r.due_at, r.estimated_days, r.id)
            items.append(
                WorklistItem(
                    type="step_to_validate",
                    case_id=r.case_id,
                    client_name=f"{r.first_name} {r.last_name}".strip(),
                    dest_country=r.dest_country,
                    title=resolve_i18n(r.step_name_i18n, lang, agency_default, r.step_name)
                    or r.step_name,
                    occurred_at=r.last_provided_at or r.updated_at,
                    is_overdue=days is not None,
                    days_late=days,
                    progress_id=r.id,
                )
            )

        # 2) Late steps on MY plate (responsible side) not already queued.
        for r in open_rows:
            if r.id in validated_ids or r.responsible_agent_id != agent.id:
                continue
            days, target = late_days(r.due_at, r.estimated_days, r.id)
            if days is None:
                continue
            assert target is not None  # days_late implies a resolved target
            items.append(
                WorklistItem(
                    type="step_overdue",
                    case_id=r.case_id,
                    client_name=f"{r.first_name} {r.last_name}".strip(),
                    dest_country=r.dest_country,
                    title=resolve_i18n(r.step_name_i18n, lang, agency_default, r.step_name)
                    or r.step_name,
                    occurred_at=target,
                    is_overdue=True,
                    days_late=days,
                    progress_id=r.id,
                )
            )

        # 3) Client documents never reviewed (validation_status NULL).
        for d in await repo.documents_to_review(agent.agency_id, agent.id):
            items.append(
                WorklistItem(
                    type="document_to_review",
                    case_id=d.case_id,
                    client_name=f"{d.first_name} {d.last_name}".strip(),
                    dest_country=d.dest_country,
                    title=d.filename,
                    occurred_at=d.created_at,
                    is_overdue=False,
                    days_late=None,
                    document_id=d.id,
                )
            )

        # 4) Reminders awaiting approval on the cases I own; one past its
        # scheduled_at is LATE (it cannot leave without me).
        for r in await repo.reminders_to_approve(agent.agency_id, agent.id):
            days = (today - r.scheduled_at.date()).days if r.scheduled_at <= now else None
            items.append(
                WorklistItem(
                    type="reminder_to_approve",
                    case_id=r.case_id,
                    client_name=f"{r.first_name} {r.last_name}".strip(),
                    dest_country=r.dest_country,
                    title=(r.message_body or "").strip()[:80] or r.channel,
                    occurred_at=r.scheduled_at,
                    is_overdue=days is not None,
                    days_late=days,
                    reminder_id=r.id,
                )
            )

        # Contract sort: overdue first (largest delay first), then the
        # oldest waiting first.
        items.sort(key=lambda i: (not i.is_overdue, -(i.days_late or 0), i.occurred_at, i.title))
        counts: dict[str, int] = defaultdict(int)
        for item in items:
            counts[item.type] += 1
        counts["total"] = len(items)
        return WorklistResponse(items=items, counts=dict(counts))


# usage_event types -> the bento vocabulary the front renders.
_ACTIVITY_LABELS = {
    "document.added": "documents_uploaded",
    "message.sent": "comment_added",
    "case.client_account_activated": "account_activated",
    "case.step_validated": "step_validated",
}
_ACTIVITY_WINDOW_DAYS = 14


class ActivityManager(DashboardManager):
    """GET /dashboard/activity - the "Activite des clients" bento feed:
    the AGENCY-WIDE client pulse (not just my cases), same gate as the
    worklist. Aggregated per (type, case, day) in SQL, 14 sliding days,
    15 items max."""

    async def get_activity(self, agent: Agent) -> ActivityResponse:
        since = datetime.now(UTC) - timedelta(days=_ACTIVITY_WINDOW_DAYS)
        rows = await ActivityRepository(self.db).client_activity(agent.agency_id, since)
        return ActivityResponse(
            items=[
                ActivityItem(
                    type=_ACTIVITY_LABELS[r.event_type],
                    case_id=r.case_id,
                    client_name=f"{r.first_name} {r.last_name}".strip(),
                    expat_user_id=r.principal_expat_user_id,
                    count=r.count,
                    occurred_at=r.occurred_at,
                )
                for r in rows
            ]
        )

"""Per-case URGENCY for the Dossiers list — THE SAME rule as the dashboard
worklist (dashboard_manager.get_worklist / progress._deadline_counter), lifted
from per-STEP to per-CASE. A case is, priority-ordered:

    OVERDUE         if ANY of its steps is overdue,
    TO_VALIDATE     else if ANY step is IN_PROGRESS awaiting the AGENCY's
                    validation (validated_by_type = 'agent'),
    AWAITING_CLIENT else if the case status is AWAITING_DOCUMENTS,
    NEUTRAL         otherwise.

FACTORISATION (single source, no divergent copy): the two step-level predicates
below encode the SAME rule the worklist uses —
  * `_step_overdue_predicate` mirrors `_deadline_counter` EXACTLY: firm `due_at`
    wins, else `started_at + estimated_days`, compared on WHOLE DAYS
    (`::date < today`); a DONE step is never overdue. `started_at` is the first
    `step.started` (MIN over activity_log), identical to
    ProgressRepository.started_ats.
  * `_step_to_validate_predicate` mirrors WorklistRepository.steps_to_validate
    (IN_PROGRESS + validated_by_type = 'agent').
The worklist stays the per-action Python authority; a consistency test
(test_case_urgency) pins list-overdue == worklist-overdue on shared fixtures so
the two evaluation engines (SQL here, Python there) can never drift.

PERF: `case_urgency_subquery` is ONE aggregate (`bool_or` grouped by case_id)
over case_step_progress — no per-case query, no N+1. The list LEFT JOINs it once
and reuses it for display, sort and filter.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy import Date, String, and_, cast, func, or_, select
from sqlalchemy import case as sa_case
from sqlalchemy.sql import ColumnElement, Subquery

from shared.models.activity import ActivityLog
from shared.models.case_step_progress import CaseStepProgress
from shared.models.journey import JourneyTemplateStep
from src.core.enums import CaseStatus, CaseUrgency, StepStatus, StepValidatorType


def _step_to_validate_predicate() -> ColumnElement[bool]:
    """A step awaiting the AGENCY's validation (worklist `step_to_validate`)."""
    return and_(
        CaseStepProgress.status == StepStatus.IN_PROGRESS.value,
        CaseStepProgress.validated_by_type == StepValidatorType.AGENT.value,
    )


def _step_overdue_predicate(started_at: ColumnElement[Any], today: date) -> ColumnElement[bool]:
    """A step past its deadline — EXACT mirror of `_deadline_counter`:
    firm due_at wins; else started_at + estimated_days; whole-day compare; a
    DONE step is never overdue; no gauge (both paths absent) => not overdue."""
    return and_(
        CaseStepProgress.status != StepStatus.DONE.value,
        or_(
            and_(
                CaseStepProgress.due_at.isnot(None),
                cast(CaseStepProgress.due_at, Date) < today,
            ),
            and_(
                CaseStepProgress.due_at.is_(None),
                JourneyTemplateStep.estimated_days.isnot(None),
                started_at.isnot(None),
                cast(
                    started_at + func.make_interval(0, 0, 0, JourneyTemplateStep.estimated_days),
                    Date,
                )
                < today,
            ),
        ),
    )


def case_urgency_subquery(today: date) -> Subquery:
    """(case_id, has_overdue, has_to_validate) — one aggregate over all steps,
    grouped by case. `started_at` comes from ONE grouped MIN over activity_log
    (`step.started` per progress, same key as ProgressRepository.started_ats),
    LEFT JOINed in — no correlated per-step subquery, one hash join."""
    pid = ActivityLog.details["step_progress_id"].astext
    started = (
        select(
            pid.label("pid"),
            func.min(ActivityLog.created_at).label("started_at"),
        )
        .where(ActivityLog.action_type == "step.started")
        .group_by(pid)
        .subquery()
    )
    return (
        select(
            CaseStepProgress.case_id.label("case_id"),
            func.bool_or(_step_overdue_predicate(started.c.started_at, today)).label("has_overdue"),
            func.bool_or(_step_to_validate_predicate()).label("has_to_validate"),
        )
        .join(
            JourneyTemplateStep,
            JourneyTemplateStep.id == CaseStepProgress.template_step_id,
        )
        .outerjoin(started, started.c.pid == cast(CaseStepProgress.id, String))
        .group_by(CaseStepProgress.case_id)
        .subquery()
    )


def urgency_value_expr(urg: Subquery, status_col: Any) -> ColumnElement[str]:
    """The CaseUrgency string, priority-ordered (overdue > to_validate >
    awaiting_client > neutral). NULL flags (case with no steps) => coalesced."""
    return sa_case(
        (func.coalesce(urg.c.has_overdue, False), CaseUrgency.OVERDUE.value),
        (func.coalesce(urg.c.has_to_validate, False), CaseUrgency.TO_VALIDATE.value),
        (status_col == CaseStatus.AWAITING_DOCUMENTS.value, CaseUrgency.AWAITING_CLIENT.value),
        else_=CaseUrgency.NEUTRAL.value,
    )


def urgency_rank_expr(urg: Subquery, status_col: Any) -> ColumnElement[int]:
    """Sort key: 0 overdue (top) .. 3 neutral. Same priority as the value."""
    return sa_case(
        (func.coalesce(urg.c.has_overdue, False), 0),
        (func.coalesce(urg.c.has_to_validate, False), 1),
        (status_col == CaseStatus.AWAITING_DOCUMENTS.value, 2),
        else_=3,
    )

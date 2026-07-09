"""Superadmin agencies table — ONE main query with correlated scalar
subqueries (cases/members/seats become sortable, paginable COLUMNS, so
`sort=cases_count` needs no extra query) + ONE COUNT for the total.
TWO constant queries whatever the page size — proven by a query-count
test. No N+1."""

from collections.abc import Sequence
from typing import Any

from sqlalchemy import Row, ScalarSelect, asc, desc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agency import Agency
from shared.models.agent import Agent
from shared.models.client_case import ClientCase

# Whitelisted sort keys → (label used both in SELECT and ORDER BY).
_SORT_KEYS = ("created_at", "name", "cases_count")


class AdminRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    def _cases_count(self) -> ScalarSelect[int]:
        return (
            select(func.count(ClientCase.id))
            .where(
                ClientCase.agency_id == Agency.id,
                ClientCase.deleted_at.is_(None),
                ClientCase.is_demo.is_(False),
            )
            .correlate(Agency)
            .scalar_subquery()
        )

    def _members_count(self) -> ScalarSelect[int]:
        return (
            select(func.count(Agent.id))
            .where(Agent.agency_id == Agency.id)
            .correlate(Agency)
            .scalar_subquery()
        )

    def _seats_used(self) -> ScalarSelect[int]:
        return (
            select(func.count(Agent.id))
            .where(Agent.agency_id == Agency.id, Agent.is_external.is_(False))
            .correlate(Agency)
            .scalar_subquery()
        )

    async def list_agencies_page(
        self, *, search: str | None, sort: str, order: str, page: int, page_size: int
    ) -> tuple[Sequence[Row[Any]], int]:
        cases_count = self._cases_count().label("cases_count")
        members_count = self._members_count().label("members_count")
        seats_used = self._seats_used().label("seats_used")

        stmt = select(
            Agency.id,
            Agency.name,
            Agency.slug,
            Agency.logo_path,
            Agency.plan,
            Agency.is_founding,
            Agency.trial_ends_at,
            Agency.converted_at,
            Agency.created_at,
            cases_count,
            members_count,
            seats_used,
        )
        count_stmt = select(func.count()).select_from(Agency)
        if search:
            like = f"%{search}%"
            predicate = or_(Agency.name.ilike(like), Agency.slug.ilike(like))
            stmt = stmt.where(predicate)
            count_stmt = count_stmt.where(predicate)

        # `sort` is whitelisted to a key that is always present, so this is a
        # total lookup (never None) — index, don't `.get()`, to keep the type.
        sort_key = sort if sort in _SORT_KEYS else "created_at"
        sort_col = {
            "created_at": Agency.created_at,
            "name": Agency.name,
            "cases_count": cases_count,
        }[sort_key]
        direction = desc if order == "desc" else asc
        # A stable tiebreaker (id) so pagination never repeats/drops a row.
        stmt = (
            stmt.order_by(direction(sort_col), Agency.id)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )

        rows = (await self.db.execute(stmt)).all()
        total = (await self.db.execute(count_stmt)).scalar_one()
        return rows, total

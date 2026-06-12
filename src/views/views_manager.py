"""Saved views — ported from Prism (src/views/views_manager), adapted
to Nidria's auth model: no fine-grained views.* permissions (the
binding gates on case.view, arbitrage Q4) — ownership is the rule:
shared views are visible to the whole agency, mutable by their owner
only."""

import uuid

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from shared.models.saved_view import SavedView
from src.core.exceptions import ForbiddenError, NotFoundError, ValidationError
from src.views.views_repository import ViewsRepository
from src.views.views_schema import (
    CASE_COLUMNS,
    DEFAULT_ALL_ENTITIES,
    AvailableColumnsResponse,
    SavedViewCreate,
    SavedViewDefaultAllUpdate,
    SavedViewRead,
    SavedViewUpdate,
)


def _to_read(view: SavedView, current_agent_id: uuid.UUID) -> SavedViewRead:
    return SavedViewRead(
        id=view.id,
        agency_id=view.agency_id,
        agent_id=view.agent_id,
        agent_name=(f"{view.agent.first_name} {view.agent.last_name}" if view.agent else ""),
        name=view.name,
        entity=view.entity,
        filters=dict(view.filters or {}),
        columns=list(view.columns) if view.columns else None,
        column_sizing=dict(view.column_sizing) if view.column_sizing else None,
        sort_by=view.sort_by,
        sort_order=view.sort_order,
        is_default=view.is_default,
        is_default_all=view.is_default_all,
        is_shared=view.is_shared,
        is_mine=view.agent_id == current_agent_id,
        created_at=view.created_at,
        updated_at=view.updated_at,
    )


def _sentinel_all_name(entity: str) -> str:
    """Server-controlled `name` for a customizable "All" row — the
    `__all__:` prefix keeps it clear of any view a user literally
    names "All". Uniqueness is the partial index's job, not the name's."""
    return f"__all__:{entity}"


def _reject_if_default_all(view: SavedView) -> None:
    """The customizable "All" rows are managed exclusively through the
    /views/default-all endpoints — the generic CRUD and set-default
    routes refuse to touch them (Prism guard, ported verbatim)."""
    if view.is_default_all:
        raise ValidationError(
            "Customized 'All' views can only be modified via the "
            "dedicated /views/default-all endpoints."
        )


class ViewsManager:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.repo = ViewsRepository(db)

    # --- columns catalog ---------------------------------------------------------

    def list_available_columns(self) -> AvailableColumnsResponse:
        return AvailableColumnsResponse(columns=list(CASE_COLUMNS))

    # --- named views ---------------------------------------------------------------

    async def list_(self, agent: Agent, entity: str | None = None) -> list[SavedViewRead]:
        views = await self.repo.list_for_agent(agent.agency_id, agent.id, entity)
        return [_to_read(view, agent.id) for view in views]

    async def create(self, agent: Agent, request: SavedViewCreate) -> SavedViewRead:
        if request.entity in DEFAULT_ALL_ENTITIES:
            raise ValidationError("The customizable 'All' view is managed via /views/default-all.")
        view = SavedView(
            agency_id=agent.agency_id,
            agent_id=agent.id,
            name=request.name,
            entity=request.entity,
            filters=request.filters,
            columns=request.columns,
            column_sizing=request.column_sizing,
            sort_by=request.sort_by,
            sort_order=request.sort_order,
            is_shared=request.is_shared,
        )
        self.db.add(view)
        await self.db.commit()
        full = await self.repo.get_by_id(agent.agency_id, view.id)
        assert full is not None
        return _to_read(full, agent.id)

    async def _get_owned(self, agent: Agent, view_id: uuid.UUID) -> SavedView:
        """Mutation target: must exist in the agency AND belong to the
        caller — a shared view stays visible to everyone but mutable by
        its owner only (Prism semantics)."""
        view = await self.repo.get_by_id(agent.agency_id, view_id)
        if view is None:
            raise NotFoundError(f"View {view_id} not found.")
        _reject_if_default_all(view)
        if view.agent_id != agent.id:
            raise ForbiddenError("Only the owner of a view can modify it.")
        return view

    async def update(
        self, agent: Agent, view_id: uuid.UUID, request: SavedViewUpdate
    ) -> SavedViewRead:
        view = await self._get_owned(agent, view_id)
        provided = request.model_fields_set
        if "name" in provided and request.name is not None:
            # Duplicate names are allowed — no collision check (Prism).
            view.name = request.name
        if "filters" in provided and request.filters is not None:
            view.filters = request.filters
        if "columns" in provided:
            view.columns = request.columns
        if "column_sizing" in provided:
            # Full replace; the frontend clears overrides by sending null.
            view.column_sizing = request.column_sizing
        if "sort_by" in provided:
            view.sort_by = request.sort_by
        if "sort_order" in provided:
            view.sort_order = request.sort_order
        if "is_shared" in provided and request.is_shared is not None:
            view.is_shared = request.is_shared
        await self.db.commit()
        full = await self.repo.get_by_id(agent.agency_id, view.id)
        assert full is not None
        return _to_read(full, agent.id)

    async def delete(self, agent: Agent, view_id: uuid.UUID) -> None:
        view = await self._get_owned(agent, view_id)
        await self.db.delete(view)
        await self.db.commit()

    async def set_default(self, agent: Agent, view_id: uuid.UUID) -> SavedViewRead:
        """Per-agent default: each agent picks their own default view
        per entity — own views or shared ones (Prism per-user
        semantics). Unsets any previous default for the same entity."""
        view = await self.repo.get_by_id(agent.agency_id, view_id)
        if view is None:
            raise NotFoundError(f"View {view_id} not found.")
        _reject_if_default_all(view)
        if view.agent_id != agent.id and not view.is_shared:
            raise ForbiddenError("Can only default your own views or shared views.")
        await self.db.execute(
            update(SavedView)
            .where(
                SavedView.agency_id == agent.agency_id,
                SavedView.agent_id == agent.id,
                SavedView.entity == view.entity,
                SavedView.is_default.is_(True),
            )
            .values(is_default=False)
        )
        view.is_default = True
        await self.db.commit()
        full = await self.repo.get_by_id(agent.agency_id, view.id)
        assert full is not None
        return _to_read(full, agent.id)

    async def unset_default(self, agent: Agent, view_id: uuid.UUID) -> SavedViewRead:
        view = await self.repo.get_by_id(agent.agency_id, view_id)
        if view is None:
            raise NotFoundError(f"View {view_id} not found.")
        _reject_if_default_all(view)
        if view.agent_id != agent.id and not view.is_shared:
            raise ForbiddenError("Can only default your own views or shared views.")
        view.is_default = False
        await self.db.commit()
        full = await self.repo.get_by_id(agent.agency_id, view.id)
        assert full is not None
        return _to_read(full, agent.id)

    # --- customizable "All" ----------------------------------------------------------

    @staticmethod
    def _validate_all_entity(entity: str) -> None:
        if entity not in DEFAULT_ALL_ENTITIES:
            raise ValidationError(
                f"Invalid default-all entity {entity!r} — allowed: {DEFAULT_ALL_ENTITIES}."
            )

    async def get_default_all(self, agent: Agent, entity: str) -> SavedViewRead | None:
        self._validate_all_entity(entity)
        view = await self.repo.get_default_all(agent.agency_id, agent.id, entity)
        return _to_read(view, agent.id) if view is not None else None

    async def upsert_default_all(
        self, agent: Agent, entity: str, request: SavedViewDefaultAllUpdate
    ) -> SavedViewRead:
        """Save-from-"All": first save creates the row, later saves
        update it. The partial unique index guarantees at most one."""
        self._validate_all_entity(entity)
        view = await self.repo.get_default_all(agent.agency_id, agent.id, entity)
        if view is None:
            view = SavedView(
                agency_id=agent.agency_id,
                agent_id=agent.id,
                name=_sentinel_all_name(entity),
                entity=entity,
                is_default_all=True,
            )
            self.db.add(view)
        view.filters = request.filters
        view.columns = request.columns
        view.column_sizing = request.column_sizing
        view.sort_by = request.sort_by
        view.sort_order = request.sort_order
        await self.db.commit()
        full = await self.repo.get_default_all(agent.agency_id, agent.id, entity)
        assert full is not None
        return _to_read(full, agent.id)

    async def reset_default_all(self, agent: Agent, entity: str) -> None:
        """Idempotent reset back to the pristine zero-filter "All"."""
        self._validate_all_entity(entity)
        view = await self.repo.get_default_all(agent.agency_id, agent.id, entity)
        if view is not None:
            await self.db.delete(view)
            await self.db.commit()

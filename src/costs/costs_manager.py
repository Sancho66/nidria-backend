import uuid
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agency import Agency
from shared.models.agent import Agent
from shared.models.case_step_cost import CaseStepCost
from shared.models.client_case import ClientCase
from src.activity.activity_manager import ActivityManager
from src.cases.cases_repository import CasesRepository
from src.core.enums import ActorType
from src.core.exceptions import NotFoundError
from src.costs.costs_repository import CostsRepository
from src.costs.costs_rules import check_amount_decimals, require_agency_currency
from src.costs.costs_schema import (
    CaseCostsResponse,
    CostLineCreateRequest,
    CostLineResponse,
    CostLineUpdateRequest,
)


class CostsManager:
    """Agency-internal cost tracking. EVERY entry is scoped to the agent's own
    agency via get_case_in_agency (a case of agency B is a 404 for agency A);
    the total is COMPUTED here (a Decimal sum), never stored. Mutations are
    traced in activity_log (cost.added / cost.edited / cost.deleted)."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.repo = CostsRepository(db)
        self.cases = CasesRepository(db)
        self.activity = ActivityManager(db)

    async def _case(self, agent: Agent, case_id: uuid.UUID) -> ClientCase:
        case = await self.cases.get_case_in_agency(agent.agency_id, case_id)
        if case is None:
            raise NotFoundError("Case not found.")
        return case

    async def _require_currency(self, agent: Agent) -> str:
        # One rule, one code — shared with template planned costs (costs_rules).
        return await require_agency_currency(self.db, agent.agency_id)

    def _check_decimals(self, amount: Decimal, currency: str) -> None:
        check_amount_decimals(amount, currency)

    def _log(self, case_id: uuid.UUID, agent: Agent, action: str, line: CaseStepCost) -> None:
        self.activity.log_action(
            case_id=case_id,
            actor_type=ActorType.AGENT,
            actor_id=agent.id,
            action_type=action,
            details={
                "cost_id": str(line.id),
                "step_progress_id": str(line.case_step_progress_id),
                # A planned-but-unpaid line has no real amount yet.
                "amount": str(line.amount) if line.amount is not None else None,
                "label": line.label,
            },
        )

    async def list_costs(self, agent: Agent, case_id: uuid.UUID) -> CaseCostsResponse:
        case = await self._case(agent, case_id)
        lines = await self.repo.list_for_case(case.id)
        # THREE totals, all computed at READ, never materialized. Decimal sums —
        # no float ever. planned ignores lines without a plan; real ignores
        # unpaid lines; variance sums (real − planned) only where BOTH exist.
        planned_total = sum(
            (line.planned_amount for line in lines if line.planned_amount is not None),
            Decimal(0),
        )
        real_total = sum((line.amount for line in lines if line.amount is not None), Decimal(0))
        variance = sum(
            (
                line.amount - line.planned_amount
                for line in lines
                if line.amount is not None and line.planned_amount is not None
            ),
            Decimal(0),
        )
        agency = await self.db.get(Agency, agent.agency_id)
        return CaseCostsResponse(
            currency=agency.currency if agency else None,
            planned_total=planned_total,
            real_total=real_total,
            variance=variance,
            lines=[CostLineResponse.model_validate(line) for line in lines],
        )

    async def add_cost(
        self,
        agent: Agent,
        case_id: uuid.UUID,
        progress_id: uuid.UUID,
        payload: CostLineCreateRequest,
    ) -> CostLineResponse:
        case = await self._case(agent, case_id)
        currency = await self._require_currency(agent)
        self._check_decimals(payload.amount, currency)
        progress = await self.repo.get_progress_in_case(case.id, progress_id)
        if progress is None:
            raise NotFoundError("Case step not found.")
        line = self.repo.add_line(
            case_step_progress_id=progress.id,
            amount=payload.amount,
            label=payload.label,
            incurred_on=payload.incurred_on,
            author_agent_id=agent.id,
        )
        await self.db.flush()
        self._log(case.id, agent, "cost.added", line)
        await self.db.commit()
        await self.db.refresh(line)
        return CostLineResponse.model_validate(line)

    async def update_cost(
        self, agent: Agent, case_id: uuid.UUID, cost_id: uuid.UUID, payload: CostLineUpdateRequest
    ) -> CostLineResponse:
        case = await self._case(agent, case_id)
        line = await self.repo.get_line_in_case(case.id, cost_id)
        if line is None:
            raise NotFoundError("Cost line not found.")
        data = payload.model_dump(exclude_unset=True)
        if "amount" in data and data["amount"] is not None:
            self._check_decimals(data["amount"], await self._require_currency(agent))
            line.amount = data["amount"]
        if "label" in data and data["label"] is not None:
            line.label = data["label"]
        if "incurred_on" in data:
            line.incurred_on = data["incurred_on"]
        self._log(case.id, agent, "cost.edited", line)
        await self.db.commit()
        await self.db.refresh(line)
        return CostLineResponse.model_validate(line)

    async def delete_cost(self, agent: Agent, case_id: uuid.UUID, cost_id: uuid.UUID) -> None:
        case = await self._case(agent, case_id)
        line = await self.repo.get_line_in_case(case.id, cost_id)
        if line is None:
            raise NotFoundError("Cost line not found.")
        self._log(case.id, agent, "cost.deleted", line)
        await self.db.delete(line)
        await self.db.commit()

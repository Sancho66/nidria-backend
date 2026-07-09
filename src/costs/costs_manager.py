import uuid
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agency import Agency
from shared.models.agent import Agent
from shared.models.case_step_cost import CaseStepCost
from shared.models.client_case import ClientCase
from src.activity.activity_manager import ActivityManager
from src.cases.cases_repository import CasesRepository
from src.core.currencies import decimals_for
from src.core.enums import ActorType
from src.core.exceptions import ConflictError, NotFoundError, ValidationError
from src.costs.costs_repository import CostsRepository
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
        """A cost has no meaning without a unit: refuse to record one until the
        agency has set its currency (a 300 with no currency is a landmine)."""
        agency = await self.db.get(Agency, agent.agency_id)
        currency = agency.currency if agency else None
        if currency is None:
            raise ConflictError(
                "Set your agency currency in the settings before recording costs.",
                code="cost.currency_required",
            )
        return currency

    def _check_decimals(self, amount: Decimal, currency: str) -> None:
        """The currency constrains what ENTERS (not what is stored): guaraní
        rejects 120.50, euro rejects 120.505, the Tunisian dinar accepts it."""
        allowed = decimals_for(currency)
        exponent = amount.as_tuple().exponent
        places = -exponent if isinstance(exponent, int) and exponent < 0 else 0
        if places > allowed:
            raise ValidationError(
                f"{currency} allows at most {allowed} decimal place(s).",
                code="cost.amount_decimals",
            )

    def _log(self, case_id: uuid.UUID, agent: Agent, action: str, line: CaseStepCost) -> None:
        self.activity.log_action(
            case_id=case_id,
            actor_type=ActorType.AGENT,
            actor_id=agent.id,
            action_type=action,
            details={
                "cost_id": str(line.id),
                "step_progress_id": str(line.case_step_progress_id),
                "amount": str(line.amount),
                "label": line.label,
            },
        )

    async def list_costs(self, agent: Agent, case_id: uuid.UUID) -> CaseCostsResponse:
        case = await self._case(agent, case_id)
        lines = await self.repo.list_for_case(case.id)
        # Computed at READ, never materialized. Decimal sum — no float ever.
        total = sum((line.amount for line in lines), Decimal(0))
        agency = await self.db.get(Agency, agent.agency_id)
        return CaseCostsResponse(
            currency=agency.currency if agency else None,
            total=total,
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

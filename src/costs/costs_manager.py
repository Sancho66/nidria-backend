import uuid
from collections import defaultdict
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
from src.costs.costs_rules import check_amount_decimals, line_variance, resolve_cost_currency
from src.costs.costs_schema import (
    CaseCostsResponse,
    CostLineCreateRequest,
    CostLineResponse,
    CostLineUpdateRequest,
    CurrencyTotals,
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
                "currency": line.currency,
                "label": line.label,
            },
        )

    async def list_costs(self, agent: Agent, case_id: uuid.UUID) -> CaseCostsResponse:
        case = await self._case(agent, case_id)
        lines = await self.repo.list_for_case(case.id)
        # Totals GROUPED BY CURRENCY, computed at read, never summed across
        # currencies (no rate). planned by planned_currency, real by (real)
        # currency; variance sums the per-line écarts (line_variance), which
        # exist only when a line's two currencies match — the invariant that
        # keeps the per-line and per-currency-total views identical.
        planned_by: defaultdict[str, Decimal] = defaultdict(Decimal)
        real_by: defaultdict[str, Decimal] = defaultdict(Decimal)
        var_by: defaultdict[str, Decimal] = defaultdict(Decimal)
        # Per planned-currency: lines planned here but PAID in another currency —
        # they lower this currency's real_total without being unpaid.
        cross_by: defaultdict[str, int] = defaultdict(int)
        currencies: set[str] = set()
        for line in lines:
            if line.planned_amount is not None and line.planned_currency is not None:
                planned_by[line.planned_currency] += line.planned_amount
                currencies.add(line.planned_currency)
                if line.amount is not None and line.currency != line.planned_currency:
                    cross_by[line.planned_currency] += 1
            if line.amount is not None:
                real_by[line.currency] += line.amount
                currencies.add(line.currency)
            v = line_variance(
                line.amount, line.currency, line.planned_amount, line.planned_currency
            )
            if v is not None:
                var_by[line.currency] += v  # currency == planned_currency here
        totals = [
            CurrencyTotals(
                currency=c,
                planned_total=planned_by[c],
                real_total=real_by[c],
                variance=var_by[c],
                planned_paid_in_other_currency=cross_by[c],
            )
            for c in sorted(currencies)
        ]
        agency = await self.db.get(Agency, agent.agency_id)
        return CaseCostsResponse(
            default_currency=agency.currency if agency else None,
            totals=totals,
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
        # The line's currency: chosen, else the agency default; 409 if neither.
        currency = await resolve_cost_currency(self.db, agent.agency_id, payload.currency)
        check_amount_decimals(payload.amount, currency)
        progress = await self.repo.get_progress_in_case(case.id, progress_id)
        if progress is None:
            raise NotFoundError("Case step not found.")
        # A manual débours: real amount + its currency, NO plan (planned_* NULL).
        line = self.repo.add_line(
            case_step_progress_id=progress.id,
            amount=payload.amount,
            currency=currency,
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
        # Resolve the effective (amount, currency) and VALIDATE before mutating —
        # a currency change can invalidate an already-entered amount's decimals.
        new_currency = data["currency"] if data.get("currency") is not None else line.currency
        new_amount = data["amount"] if data.get("amount") is not None else line.amount
        if new_amount is not None:
            check_amount_decimals(new_amount, new_currency)
        line.currency = new_currency
        line.amount = new_amount
        if data.get("label") is not None:
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

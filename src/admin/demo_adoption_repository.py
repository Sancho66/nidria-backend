"""Aggregate actual records; never interpret seeded fixtures as adoption."""

from typing import Any

from sqlalchemy import exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agency import Agency
from shared.models.agent import Agent
from shared.models.case_person import CasePerson
from shared.models.client_case import ClientCase
from shared.models.client_profile import ClientProfile
from shared.models.journey import JourneyTemplate
from src.admin.demo_adoption_schema import DemoAdoptionQuery
from src.demo_sandbox.measurement import demo_attribution


def adoption_source() -> Any:
    journeys = select(JourneyTemplate).where(
        JourneyTemplate.agency_id == Agency.id,
        JourneyTemplate.origin == "user",
        JourneyTemplate.is_sample.is_(False),
    )
    cases = select(ClientCase).where(
        ClientCase.agency_id == Agency.id,
        ClientCase.is_demo.is_(False),
        ClientCase.deleted_at.is_(None),
    )
    demo_principal = exists().where(
        ClientCase.agency_id == ClientProfile.agency_id,
        ClientCase.is_demo.is_(True),
        ClientCase.principal_expat_user_id == ClientProfile.expat_user_id,
    )
    demo_person = exists(
        select(CasePerson.id)
        .join(ClientCase, ClientCase.id == CasePerson.case_id)
        .where(
            ClientCase.agency_id == ClientProfile.agency_id,
            ClientCase.is_demo.is_(True),
            or_(
                CasePerson.client_profile_id == ClientProfile.id,
                CasePerson.expat_user_id == ClientProfile.expat_user_id,
            ),
        )
    )
    clients = select(ClientProfile).where(
        ClientProfile.agency_id == Agency.id,
        ~demo_principal,
        ~demo_person,
    )
    counts = [
        statement.with_only_columns(func.count(model.id))
        .correlate(Agency)
        .scalar_subquery()
        .label(name)
        for name, model, statement in (
            ("journeys", JourneyTemplate, journeys),
            ("clients", ClientProfile, clients),
            ("cases", ClientCase, cases),
        )
    ]
    latest = func.greatest(
        *[
            statement.with_only_columns(func.max(model.created_at))
            .correlate(Agency)
            .scalar_subquery()
            for model, statement in (
                (JourneyTemplate, journeys),
                (ClientProfile, clients),
                (ClientCase, cases),
            )
        ]
    ).label("last_creation_at")
    last_login = (
        select(func.max(Agent.last_login_at))
        .where(Agent.agency_id == Agency.id, Agent.is_external.is_(False))
        .correlate(Agency)
        .scalar_subquery()
    )
    return select(
        Agency.id.label("agency_id"),
        Agency.name,
        func.coalesce(Agency.utm_campaign, "").label("batch"),
        Agency.utm_content.label("reference"),
        Agency.created_at.label("registered_at"),
        last_login.label("last_login_at"),
        latest,
        *counts,
    ).where(
        Agency.utm_source == "demo",
        Agency.is_internal.is_(False),
        ~Agency.slug.startswith("demo-sandbox-"),
    )


class DemoAdoptionRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def page(self, query: DemoAdoptionQuery) -> dict[str, Any]:
        source = adoption_source().subquery()
        conditions = []
        if query.search:
            conditions.append(source.c.name.icontains(query.search, autoescape=True))
        if query.batch:
            conditions.append(source.c.batch == query.batch)
        if query.demo:
            conditions.append(source.c.reference == demo_attribution(query.demo))
        if query.activity == "none":
            conditions.extend(source.c[key] == 0 for key in ("journeys", "clients", "cases"))
        elif query.activity != "all":
            conditions.append(source.c[query.activity] > 0)
        filtered = select(source).where(*conditions).cte("demo_adoption")
        totals = (
            (
                await self.db.execute(
                    select(
                        func.count().label("accounts"),
                        *[
                            func.count().filter(filtered.c[key] > 0).label("with_" + key)
                            for key in ("journeys", "clients", "cases")
                        ],
                    ).select_from(filtered)
                )
            )
            .mappings()
            .one()
        )
        order = [filtered.c.registered_at.desc(), filtered.c.agency_id]
        if query.sort == "activity":
            order = [filtered.c.last_creation_at.desc().nulls_last(), *order]
        elif query.sort != "recent":
            order = [filtered.c[query.sort].desc(), *order]
        rows = (
            (
                await self.db.execute(
                    select(filtered)
                    .order_by(*order)
                    .offset((query.page - 1) * query.page_size)
                    .limit(query.page_size)
                )
            )
            .mappings()
            .all()
        )
        return {"rows": rows, "totals": dict(totals)}

    async def for_references(self, references: list[str]) -> dict[str, dict[str, int]]:
        if not references:
            return {}
        source = adoption_source().where(Agency.utm_content.in_(references)).subquery()
        rows = (
            (
                await self.db.execute(
                    select(
                        source.c.reference,
                        func.count().label("accounts"),
                        *[
                            func.coalesce(func.sum(source.c[key]), 0).label(key)
                            for key in ("journeys", "clients", "cases")
                        ],
                    ).group_by(source.c.reference)
                )
            )
            .mappings()
            .all()
        )
        return {
            row["reference"]: {
                key: row[key] for key in ("accounts", "journeys", "clients", "cases")
            }
            for row in rows
        }

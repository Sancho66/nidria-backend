"""Resolve a cabinet to its only fictional client and case."""

from dataclasses import dataclass

from sqlalchemy import exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agency import Agency
from shared.models.case_person import CasePerson
from shared.models.client_case import ClientCase
from shared.models.expat_user import ExpatUser
from src.demo_sandbox.catalog import DemoRecord


@dataclass(frozen=True)
class DemoIdentity:
    case: ClientCase
    expat: ExpatUser


class DemoRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def resolve(self, record: DemoRecord, fingerprint: str) -> DemoIdentity | None:
        rows = (
            await self.db.execute(
                select(ClientCase, ExpatUser, Agency)
                .join(Agency, Agency.id == ClientCase.agency_id)
                .join(ExpatUser, ExpatUser.id == ClientCase.principal_expat_user_id)
                .where(Agency.slug == record.agency_slug, ClientCase.deleted_at.is_(None))
            )
        ).all()
        if len(rows) != 1:
            return None
        case, expat, agency = rows[0]
        if (
            not case.is_demo
            or expat.activated_at is None
            or expat.preferred_lang != record.language
            or agency.settings.get("sandbox_catalog") != fingerprint
            or agency.settings.get("sandbox_record") != record.id
        ):
            return None
        # A future administrative mistake must not expand a public link's scope.
        other_case = await self.db.scalar(
            select(
                exists().where(
                    ClientCase.id != case.id,
                    ClientCase.deleted_at.is_(None),
                    or_(
                        ClientCase.principal_expat_user_id == expat.id,
                        ClientCase.id.in_(
                            select(CasePerson.case_id).where(CasePerson.expat_user_id == expat.id)
                        ),
                    ),
                )
            )
        )
        return None if other_case else DemoIdentity(case=case, expat=expat)

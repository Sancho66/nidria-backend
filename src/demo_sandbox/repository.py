"""Resolve a cabinet to its only fictional client and case."""

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agency import Agency
from shared.models.case_person import CasePerson
from shared.models.client_case import ClientCase
from shared.models.expat_user import ExpatUser
from src.demo_sandbox.catalog import DemoCatalog, DemoRecord


@dataclass(frozen=True)
class DemoIdentity:
    case: ClientCase
    expat: ExpatUser


class DemoRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def entry(self, code: str, catalog: DemoCatalog) -> tuple[DemoRecord, str] | None:
        agency = await self.db.scalar(select(Agency).where(Agency.slug == f"demo-sandbox-{code}"))
        if agency is None or agency.settings.get("sandbox_enabled", True) is not True:
            return None
        expires_at = agency.settings.get("sandbox_expires_at")
        if expires_at and datetime.fromisoformat(expires_at) <= datetime.now(UTC):
            return None
        raw = agency.settings.get("sandbox_dynamic_entry")
        if raw is not None:
            record = DemoRecord.model_validate(raw)
            return (record, record_fingerprint(record)) if record.public_code == code else None
        legacy = catalog.by_code(code)
        return (legacy, catalog.fingerprint) if legacy else None

    async def resolve(
        self, record: DemoRecord, fingerprint: str, *, allow_disabled: bool = False
    ) -> DemoIdentity | None:
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
            or (not allow_disabled and agency.settings.get("sandbox_enabled", True) is not True)
            or expat.activated_at is None
            or expat.preferred_lang != record.language
            or agency.settings.get("sandbox_catalog") != fingerprint
            or agency.settings.get("sandbox_record") != record.id
            or (
                not allow_disabled
                and agency.settings.get("sandbox_expires_at")
                and datetime.fromisoformat(agency.settings["sandbox_expires_at"])
                <= datetime.now(UTC)
            )
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


def record_fingerprint(record: DemoRecord) -> str:
    return hashlib.sha256(
        json.dumps(record.model_dump(), sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from src.core.enums import Audience
from src.core.exceptions import NotFoundError, UnauthorizedError
from src.core.security import create_access_token
from src.demo_sandbox.catalog import DemoCatalog
from src.demo_sandbox.repository import DemoIdentity, DemoRepository
from src.demo_sandbox.schema import DemoSessionResponse
from src.demo_sandbox.storage import load_record_documents


class DemoManager:
    def __init__(self, db: AsyncSession, catalog: DemoCatalog) -> None:
        self.repo = DemoRepository(db)
        self.catalog = catalog

    async def open_session(self, code: str) -> DemoSessionResponse:
        entry = await self.repo.entry(code, self.catalog)
        record, fingerprint = entry if entry else (None, "")
        identity = await self.repo.resolve(record, fingerprint) if record else None
        if record is None or identity is None:
            raise NotFoundError("Demo not found.", code="case.not_found")
        load_record_documents(record)
        token = create_access_token(
            str(identity.expat.id),
            Audience.EXPAT,
            extra_claims={
                "demo_record": record.id,
                "demo_code": record.public_code,
                "demo_case_id": str(identity.case.id),
                "demo_catalog": fingerprint,
            },
            expires_minutes=120,
        )
        return DemoSessionResponse(
            access_token=token, case_id=identity.case.id, language=record.language
        )

    async def verify_session(self, payload: dict[str, Any]) -> DemoIdentity:
        legacy = self.catalog.by_id(str(payload.get("demo_record", "")))
        code = str(payload.get("demo_code", legacy.public_code if legacy else ""))
        entry = await self.repo.entry(code, self.catalog)
        record, fingerprint = entry if entry else (None, "")
        identity = await self.repo.resolve(record, fingerprint) if record else None
        if (
            identity is None
            or payload.get("demo_catalog") != fingerprint
            or payload.get("sub") != str(identity.expat.id)
            or payload.get("demo_case_id") != str(identity.case.id)
        ):
            raise UnauthorizedError("Invalid demo session.", code="auth.session_expired")
        if record is not None:
            load_record_documents(record)
        return identity

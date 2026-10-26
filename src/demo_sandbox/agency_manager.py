"""Issue sandbox-only sessions for one fictional agency or one of its clients."""

import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.enums import Audience
from src.core.exceptions import NotFoundError, UnauthorizedError
from src.core.security import create_access_token
from src.demo_sandbox.agency_repository import AgencyDemoIdentity, AgencyDemoRepository
from src.demo_sandbox.agency_seed import variant_record
from src.demo_sandbox.catalog import DemoRecord
from src.demo_sandbox.repository import DemoIdentity
from src.demo_sandbox.schema import DemoSessionRequest, DemoSessionResponse
from src.demo_sandbox.storage import load_record_documents


class AgencyDemoManager:
    def __init__(self, db: AsyncSession) -> None:
        self.repo = AgencyDemoRepository(db)

    async def open_session(self, body: DemoSessionRequest) -> DemoSessionResponse | None:
        identity = await self.repo.resolve(body.code)
        if identity is None:
            inactive = await self.repo.resolve(body.code, allow_disabled=True)
            if inactive is not None and datetime.fromisoformat(
                inactive.metadata["expires_at"]
            ) <= datetime.now(UTC):
                raise HTTPException(status_code=410, detail="This demonstration has expired.")
            return None
        case_id = body.case_id or uuid.UUID(identity.metadata["case_ids"][0])
        client = await self.repo.expat(identity, case_id)
        if client is None:
            raise NotFoundError("Case not found.", code="case.not_found")
        face = body.face or "agent"
        payload = {
            "demo_agency_id": str(identity.agency.id),
            "demo_code": body.code,
            "demo_full_version": identity.metadata["version"],
            "demo_case_id": str(case_id),
        }
        token = create_access_token(
            str(identity.agent.id if face == "agent" else client.id),
            Audience.AGENT if face == "agent" else Audience.EXPAT,
            extra_claims=payload,
            expires_minutes=120,
        )
        self.load_documents(identity, case_id)
        return DemoSessionResponse(
            access_token=token,
            case_id=case_id,
            language=identity.agency.default_language,
            face=face,
            agency_name=identity.agency.name,
            agency_demo=True,
            expires_at=identity.metadata["expires_at"],
        )

    def load_documents(self, identity: AgencyDemoIdentity, case_id: uuid.UUID) -> None:
        index = identity.metadata["case_ids"].index(str(case_id))
        record = DemoRecord.model_validate(identity.agency.settings["sandbox_dynamic_entry"])
        load_record_documents(variant_record(record, index))

    async def verify_agent(self, payload: dict[str, Any]) -> AgencyDemoIdentity:
        identity = await self._verify(payload)
        if payload.get("sub") != str(identity.agent.id):
            raise UnauthorizedError("Invalid demo session.", code="auth.session_expired")
        return identity

    async def verify_expat(self, payload: dict[str, Any]) -> DemoIdentity:
        identity = await self._verify(payload)
        try:
            case_id = uuid.UUID(payload.get("demo_case_id", ""))
        except (ValueError, TypeError) as exc:
            raise UnauthorizedError("Invalid demo session.", code="auth.session_expired") from exc
        client = await self.repo.expat(identity, case_id)
        if client is None or payload.get("sub") != str(client.id):
            raise UnauthorizedError("Invalid demo session.", code="auth.session_expired")
        self.load_documents(identity, case_id)
        return DemoIdentity(next(case for case in identity.cases if case.id == case_id), client)

    async def _verify(self, payload: dict[str, Any]) -> AgencyDemoIdentity:
        identity = await self.repo.resolve(str(payload.get("demo_code", "")))
        if (
            identity is None
            or payload.get("demo_agency_id") != str(identity.agency.id)
            or payload.get("demo_full_version") != identity.metadata["version"]
        ):
            raise UnauthorizedError("Invalid demo session.", code="auth.session_expired")
        return identity

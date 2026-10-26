"""Create and disable fictional dossiers, using a dedicated writer transaction."""

import hashlib
import json
import secrets
import uuid

from sqlalchemy import func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agency import Agency
from shared.models.agent import Agent
from shared.models.client_case import ClientCase
from shared.models.rbac import Role
from src.activity.activity_manager import ActivityManager
from src.admin.demo_schema import (
    DemoCreateRequest,
    DemoJourneyInput,
    DemoJourneyPreview,
    DemoListResponse,
    DemoRead,
    DemoTemplateRead,
)
from src.core.enums import ActorType
from src.core.exceptions import ConflictError, NotFoundError, ValidationError
from src.core.security import hash_password
from src.demo_sandbox.catalog import DemoCatalog, DemoRecord
from src.demo_sandbox.draft import build_record, journey_source, preview_journey
from src.demo_sandbox.repository import DemoRepository, record_fingerprint
from src.demo_sandbox.seed import seed_record
from src.journeys.import_manager import JourneyImportManager


class DemoControlManager:
    def __init__(self, db: AsyncSession, catalog: DemoCatalog) -> None:
        self.db = db
        self.catalog = catalog

    def _record(self, agency: Agency) -> DemoRecord:
        raw = agency.settings.get("sandbox_dynamic_entry")
        record = (
            DemoRecord.model_validate(raw)
            if raw
            else self.catalog.by_id(agency.settings.get("sandbox_record", ""))
        )
        if record is None or agency.slug != record.agency_slug:
            raise NotFoundError("Demo not found.", code="demo.not_found")
        return record

    def read(self, agency: Agency) -> DemoRead:
        record = self._record(agency)
        full = agency.settings.get("sandbox_full_demo", {})
        visits = agency.settings.get("sandbox_visits", {})
        return DemoRead(
            code=record.public_code,
            name=record.name,
            language=record.language,
            journey_name=record.promised_journey,
            completed_steps=record.state["progress"]["done"],
            total_steps=len(record.state["steps"]),
            public_url=f"https://app.nidria.com/demo/{record.public_code}",
            enabled=agency.settings.get("sandbox_enabled", True) is True,
            created_at=agency.created_at,
            has_logo=agency.logo_path is not None,
            scope="agency" if full else "client",
            lead_email=full.get("email"),
            expires_at=full.get("expires_at"),
            first_opened_at=visits.get("first"),
            last_opened_at=visits.get("last"),
            visits=visits.get("count", 0),
        )

    async def list_demos(self, page: int, page_size: int, search: str) -> DemoListResponse:
        conditions = [Agency.slug.startswith("demo-sandbox-")]
        if search.strip():
            query = search.strip()
            legacy_ids = [
                record.id
                for record in self.catalog.records
                if query.casefold() in record.promised_journey.casefold()
            ]
            conditions.append(
                or_(
                    Agency.name.icontains(query, autoescape=True),
                    Agency.settings["sandbox_dynamic_entry"]["promised_journey"].astext.icontains(
                        query, autoescape=True
                    ),
                    Agency.settings["sandbox_record"].astext.in_(legacy_ids),
                )
            )
        total = (
            await self.db.scalar(select(func.count()).select_from(Agency).where(*conditions)) or 0
        )
        rows = (
            await self.db.scalars(
                select(Agency)
                .where(*conditions)
                .order_by(Agency.created_at.desc(), Agency.id)
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        ).all()
        return DemoListResponse(
            items=[self.read(agency) for agency in rows],
            total=total,
            page=page,
            page_size=page_size,
        )

    def templates(self) -> list[DemoTemplateRead]:
        return [
            DemoTemplateRead(
                id=record.id,
                name=record.promised_journey,
                language=record.language,
                steps=len(record.state["steps"]),
            )
            for record in self.catalog.records
        ]

    async def preview(self, payload: DemoJourneyInput) -> DemoJourneyPreview:
        result = preview_journey(payload, self.catalog)
        # Preview needs only agency-scoped lookups; this unpersisted actor has no
        # real agency, provider or client. Nothing is inserted for a preview.
        actor = Agent(id=uuid.uuid4(), agency_id=uuid.uuid4())
        report = await JourneyImportManager(self.db).run(
            actor, journey_source(payload, self.catalog), preview=True
        )
        if report.steps_ignored or len(report.steps_created) != len(result.steps):
            raise ValidationError("Every step must be valid.", code="demo.invalid_journey")
        return result

    async def create(self, actor_id: uuid.UUID, payload: DemoCreateRequest) -> DemoRead:
        fingerprint = hashlib.sha256(
            json.dumps(payload.model_dump(mode="json"), sort_keys=True).encode()
        ).hexdigest()
        # The connection's outer transaction retains this lock even when a
        # reused business manager commits its own savepoint.
        await self.db.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": f"demo-create:{payload.request_id}"},
        )
        existing = await self.db.scalar(
            select(Agency).where(
                Agency.settings["sandbox_request_id"].astext == str(payload.request_id)
            )
        )
        if existing is not None:
            if existing.settings.get("sandbox_request_hash") != fingerprint:
                raise ConflictError(
                    "This request has already been used.", code="demo.request_conflict"
                )
            return self.read(existing)
        record = build_record(payload, secrets.token_hex(16), self.catalog)
        await self.preview(payload)
        role = (
            await self.db.scalars(select(Role).where(Role.name == "admin", Role.is_system))
        ).one()
        agency = await seed_record(
            self.db,
            record_fingerprint(record),
            record,
            role,
            hash_password(secrets.token_urlsafe(48)),
            extra_settings={
                "sandbox_dynamic_entry": record.model_dump(),
                "sandbox_enabled": True,
                "sandbox_request_id": str(payload.request_id),
                "sandbox_request_hash": fingerprint,
                "sandbox_created_by": str(actor_id),
            },
        )
        identity = await DemoRepository(self.db).resolve(record, record_fingerprint(record))
        if identity is None:
            raise RuntimeError("The created demo failed its ownership check")
        ActivityManager(self.db).log_action(
            case_id=identity.case.id,
            actor_type=ActorType.SYSTEM,
            actor_id=None,
            action_type="demo.created",
            details={
                "platform_operator": str(actor_id),
                "request_id": str(payload.request_id),
                "fictional": True,
            },
        )
        await self.db.commit()
        return self.read(agency)

    async def disable(self, actor_id: uuid.UUID, code: str) -> DemoRead:
        agency = await self.db.scalar(
            select(Agency).where(Agency.slug == f"demo-sandbox-{code}").with_for_update()
        )
        if agency is None:
            raise NotFoundError("Demo not found.", code="demo.not_found")
        self._record(agency)
        if agency.settings.get("sandbox_enabled", True):
            agency.settings = {**agency.settings, "sandbox_enabled": False}
            case = await self.db.scalar(
                select(ClientCase).where(ClientCase.agency_id == agency.id, ClientCase.is_demo)
            )
            if case is None:
                raise NotFoundError("Demo not found.", code="demo.not_found")
            ActivityManager(self.db).log_action(
                case_id=case.id,
                actor_type=ActorType.SYSTEM,
                actor_id=None,
                action_type="demo.disabled",
                details={"platform_operator": str(actor_id), "fictional": True},
            )
            await self.db.commit()
        return self.read(agency)

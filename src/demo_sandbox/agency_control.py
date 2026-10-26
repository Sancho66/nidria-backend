"""The authenticated channel creates fixtures and reuses a batch/email identity."""

import asyncio
import hashlib
import json
import secrets
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agency import Agency
from shared.models.rbac import Role
from src.admin.demo_schema import AgencyDemoBatchRequest, AgencyDemoBatchResponse
from src.core.exceptions import ConflictError
from src.core.security import hash_password
from src.demo_sandbox.agency_draft import agency_record
from src.demo_sandbox.agency_repository import AgencyDemoRepository
from src.demo_sandbox.agency_seed import populate_agency
from src.demo_sandbox.catalog import DemoCatalog
from src.demo_sandbox.control_manager import DemoControlManager
from src.demo_sandbox.people import email_domain, fictional_people
from src.demo_sandbox.repository import record_fingerprint
from src.demo_sandbox.seed import seed_record


class AgencyDemoControlManager:
    def __init__(self, db: AsyncSession, catalog: DemoCatalog) -> None:
        self.db = db
        self.catalog = catalog
        self.reused_codes: set[str] = set()

    async def create_batch(
        self, actor_id: uuid.UUID, payload: AgencyDemoBatchRequest
    ) -> AgencyDemoBatchResponse:
        result = []
        reader = DemoControlManager(self.db, self.catalog)
        for item in payload.items:
            if payload.batch.strip():
                # Shared by the old pilot endpoint and durable jobs. The lock
                # is held until the outer fictional transaction commits.
                await self.db.execute(
                    text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                    {"key": f"agency-demo-batch:{payload.batch.strip()}:{str(item.email).lower()}"},
                )
                existing_batch = await self.db.scalar(
                    select(Agency)
                    .where(
                        Agency.slug.startswith("demo-sandbox-"),
                        Agency.settings["sandbox_batch"].astext == payload.batch.strip(),
                        func.lower(Agency.settings["sandbox_full_demo"]["email"].astext)
                        == str(item.email).lower(),
                    )
                    .order_by(Agency.created_at, Agency.id)
                    .limit(1)
                )
                if existing_batch is not None:
                    previous = reader.read(existing_batch)
                    self.reused_codes.add(previous.code)
                    result.append(previous)
                    continue
            request_id = str(uuid.uuid5(payload.request_id, str(item.email).casefold()))
            content = item.model_dump(mode="json")
            # Keep retry fingerprints compatible with imports predating these
            # optional columns. Nonempty overrides remain part of the hash.
            for key in ("step_titles", "journey_name", "documents"):
                if not content[key]:
                    content.pop(key)
            if payload.batch:
                content["batch"] = payload.batch.strip()
            if payload.lifetime_days != 60:
                content["lifetime_days"] = payload.lifetime_days
            fingerprint = hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()
            await self.db.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": f"agency-demo:{request_id}"},
            )
            existing = await self.db.scalar(
                select(Agency).where(Agency.settings["sandbox_request_id"].astext == request_id)
            )
            if existing is not None:
                if existing.settings.get("sandbox_request_hash") != fingerprint:
                    raise ConflictError(
                        "This request has already been used.", code="demo.request_conflict"
                    )
                previous = reader.read(existing)
                self.reused_codes.add(previous.code)
                result.append(previous)
                continue
            record = agency_record(item, secrets.token_hex(16), self.catalog)
            people = fictional_people(item)
            domain = await email_domain(self.db, item.company_name)
            role = (
                await self.db.scalars(select(Role).where(Role.name == "admin", Role.is_system))
            ).one()
            agency = await seed_record(
                self.db,
                record_fingerprint(record),
                record,
                role,
                await asyncio.to_thread(hash_password, secrets.token_urlsafe(48)),
                owner_name=people.team[0],
                client_name=people.clients[0],
                mail_domain=domain,
                extra_settings={
                    "sandbox_dynamic_entry": record.model_dump(),
                    "sandbox_enabled": True,
                    "sandbox_request_id": request_id,
                    "sandbox_request_hash": fingerprint,
                    "sandbox_created_by": str(actor_id),
                },
            )
            metadata = await populate_agency(self.db, agency, record, item, domain)
            metadata["expires_at"] = (
                datetime.now(UTC) + timedelta(days=payload.lifetime_days)
            ).isoformat()
            metadata["version"] = fingerprint
            agency.settings = {
                **agency.settings,
                "sandbox_full_demo": metadata,
                "sandbox_batch": payload.batch.strip(),
            }
            await self.db.commit()
            if await AgencyDemoRepository(self.db).resolve(record.public_code) is None:
                raise RuntimeError("The new fictional agency failed its isolation check")
            result.append(reader.read(agency))
        return AgencyDemoBatchResponse(items=result)

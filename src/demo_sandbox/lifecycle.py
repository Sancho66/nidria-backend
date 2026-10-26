"""Isolated demo lifecycle, with export snapshots and no external adapters."""

import csv
import hashlib
import hmac
import io
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import (
    Column,
    DateTime,
    Integer,
    MetaData,
    String,
    Table,
    and_,
    any_,
    bindparam,
    cast,
    delete,
    func,
    or_,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, insert
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agency import Agency
from shared.models.agent import Agent
from shared.models.auth_tokens import PasswordResetToken, RefreshToken
from shared.models.case_person import CasePerson
from shared.models.client_case import ClientCase
from shared.models.client_profile import ClientProfile
from shared.models.expat_user import ExpatUser
from shared.models.invitation import AgentInvitation
from shared.models.mfa import MfaChallenge, MfaTotp
from src.admin.demo_schema import (
    DemoBulkRequest,
    DemoExportResponse,
    DemoFilters,
    DemoRead,
    DemoSelection,
)
from src.core.config import get_settings
from src.core.exceptions import ConflictError
from src.demo_sandbox.guards import SANDBOX_DB_NAME

# Created only by the isolated seed operator, never in production migrations.
# This tombstone keeps the requested opening history, not fictional case data.
history = Table(
    "demo_history",
    MetaData(),
    Column("code", String(32), primary_key=True),
    Column("batch", String(100), nullable=False),
    Column("snapshot", JSONB, nullable=False),
    Column("deleted_at", DateTime(timezone=True), nullable=False),
)


async def prepare_history(db: AsyncSession) -> None:
    connection = await db.connection()
    await connection.run_sync(history.metadata.create_all)
    await db.execute(text("ALTER TABLE demo_history ENABLE ROW LEVEL SECURITY"))


def expiry_expression() -> Any:
    return cast(
        func.coalesce(
            Agency.settings["sandbox_expires_at"].astext,
            Agency.settings["sandbox_full_demo"]["expires_at"].astext,
        ),
        DateTime(timezone=True),
    )


def filter_conditions(filters: DemoFilters) -> list[Any]:
    conditions: list[Any] = [Agency.slug.startswith("demo-sandbox-")]
    if filters.batch:
        conditions.append(Agency.settings["sandbox_batch"].astext == filters.batch)
    expired = expiry_expression() <= datetime.now(UTC)
    enabled = func.coalesce(Agency.settings["sandbox_enabled"].astext, "true") == "true"
    live = or_(expiry_expression().is_(None), ~expired)
    if filters.status == "active":
        conditions.append(and_(enabled, live))
    elif filters.status == "disabled":
        conditions.append(and_(~enabled, live))
    elif filters.status == "expired":
        conditions.append(expired)
    visits = cast(func.coalesce(Agency.settings["sandbox_visits"]["count"].astext, "0"), Integer)
    if filters.opened == "opened":
        conditions.append(visits > 0)
    elif filters.opened == "unopened":
        conditions.append(visits == 0)
    return conditions


def history_csv(rows: list[dict[str, Any]]) -> str:
    output = io.StringIO(newline="")
    fields = [
        "lead_email",
        "public_url",
        "name",
        "batch",
        "visits",
        "first_opened_at",
        "last_opened_at",
        "tour_status",
        "tour_last_step",
        "tour_started_at",
        "tour_completed_at",
        "expires_at",
        "status",
        "deleted_at",
    ]
    writer = csv.writer(output)
    writer.writerow(fields)
    for row in rows:
        # An operator may open the CSV in Excel. Prospect text is never a formula.
        values = ["" if row.get(key) is None else str(row[key]) for key in fields]
        writer.writerow(
            [
                "'" + value if value.startswith(("=", "+", "-", "@", "\t", "\r")) else value
                for value in values
            ]
        )
    return "\ufeff" + output.getvalue()


def selection_digest(rows: list[DemoRead]) -> str:
    # Openings may progress between export and confirmation. Membership and
    # lifecycle configuration must not change behind the displayed count.
    content = "\n".join(
        sorted(f"{row.code}:{row.batch}:{row.enabled}:{row.expires_at}" for row in rows)
    )
    return hashlib.sha256(content.encode()).hexdigest()


def selection_token(rows: list[DemoRead], actor_id: uuid.UUID, timestamp: int) -> str:
    content = f"{actor_id}:{timestamp}:{selection_digest(rows)}"
    signature = hmac.new(
        get_settings().demo_control_secret.encode(), content.encode(), hashlib.sha256
    ).hexdigest()
    return f"{timestamp}.{signature}"


def verify_selection(payload: DemoBulkRequest, rows: list[DemoRead], actor_id: uuid.UUID) -> None:
    try:
        timestamp = int(payload.selection_token.split(".", 1)[0])
    except ValueError:
        timestamp = 0
    if (
        len(rows) != payload.expected_count
        or not 0 <= time.time() - timestamp <= 900
        or not hmac.compare_digest(
            payload.selection_token, selection_token(rows, actor_id, timestamp)
        )
    ):
        raise ConflictError(
            "Export the current selection again before confirming.", code="demo.selection_changed"
        )


async def selection_rows(
    db: AsyncSession, selection: DemoSelection, *, lock: bool = False, catalog: Any = None
) -> list[Agency]:
    conditions = filter_conditions(selection.filters or DemoFilters())
    if selection.codes is not None:
        conditions.append(Agency.slug.in_([f"demo-sandbox-{code}" for code in selection.codes]))
    if selection.excluded_codes:
        conditions.append(
            ~Agency.slug.in_([f"demo-sandbox-{code}" for code in selection.excluded_codes])
        )
    if selection.filters and selection.filters.search:
        query = selection.filters.search
        legacy_ids = (
            [
                record.id
                for record in catalog.records
                if query.casefold() in record.promised_journey.casefold()
            ]
            if catalog
            else []
        )
        conditions.append(
            or_(
                Agency.name.icontains(query, autoescape=True),
                Agency.settings["sandbox_dynamic_entry"]["promised_journey"].astext.icontains(
                    query, autoescape=True
                ),
                Agency.settings["sandbox_full_demo"]["email"].astext.icontains(
                    query, autoescape=True
                ),
                Agency.settings["sandbox_record"].astext.in_(legacy_ids),
            )
        )
    statement = select(Agency).where(*conditions).order_by(Agency.id).limit(5001)
    if lock:
        statement = statement.with_for_update()
    rows = list(await db.scalars(statement))
    if len(rows) > 5000 or (selection.codes is not None and len(rows) != len(selection.codes)):
        raise ConflictError("The demo selection has changed.", code="demo.selection_changed")
    return rows


def uuid_array(values: Any) -> Any:
    # One bound array avoids PostgreSQL's parameter ceiling for large waves.
    return any_(bindparam(None, list(values), type_=ARRAY(PGUUID(as_uuid=True))))


async def erase_demos(db: AsyncSession, agencies: list[Agency], snapshots: list[DemoRead]) -> None:
    if await db.scalar(text("SELECT current_database()")) != SANDBOX_DB_NAME:
        raise RuntimeError("Demo deletion requires the isolated database")
    if len(agencies) != len(snapshots) or any(
        agency.slug != f"demo-sandbox-{snapshot.code}" or not agency.settings.get("sandbox_record")
        for agency, snapshot in zip(agencies, snapshots, strict=True)
    ):
        raise RuntimeError("Not a fictional demo agency")
    agency_ids = [agency.id for agency in agencies]
    cases = list(
        await db.scalars(select(ClientCase).where(ClientCase.agency_id == uuid_array(agency_ids)))
    )
    if any(not case.is_demo for case in cases):
        raise RuntimeError("Refusing to delete a non-demo case")
    case_ids = [case.id for case in cases]
    clients = {case.principal_expat_user_id for case in cases}
    clients.update(
        await db.scalars(
            select(CasePerson.expat_user_id).where(CasePerson.case_id == uuid_array(case_ids))
        )
    )
    clients.discard(None)
    foreign_case = await db.scalar(
        select(ClientCase.id)
        .where(
            ~(ClientCase.agency_id == uuid_array(agency_ids)),
            (ClientCase.principal_expat_user_id == uuid_array(clients)),
        )
        .limit(1)
    )
    foreign_person = await db.scalar(
        select(CasePerson.id)
        .where(
            ~(CasePerson.case_id == uuid_array(case_ids)),
            (CasePerson.expat_user_id == uuid_array(clients)),
        )
        .limit(1)
    )
    foreign_profile = await db.scalar(
        select(ClientProfile.id)
        .where(
            ~(ClientProfile.agency_id == uuid_array(agency_ids)),
            (ClientProfile.expat_user_id == uuid_array(clients)),
        )
        .limit(1)
    )
    if foreign_case or foreign_person or foreign_profile:
        raise RuntimeError("A fictional client is shared outside this demo")
    if not agencies:
        return
    await db.execute(
        insert(history).on_conflict_do_nothing(index_elements=[history.c.code]),
        [
            dict(
                code=snapshot.code,
                batch=snapshot.batch,
                snapshot=snapshot.model_dump(mode="json"),
                deleted_at=datetime.now(UTC),
            )
            for snapshot in snapshots
        ],
    )
    agent_ids = list(
        await db.scalars(select(Agent.id).where(Agent.agency_id == uuid_array(agency_ids)))
    )
    # Same ordering as AgenciesRepository.purge_agency_rows, set-based so a
    # 2,900-demo wave does not issue thousands of per-agency round trips.
    for model in (RefreshToken, PasswordResetToken, MfaTotp, MfaChallenge):
        await db.execute(
            delete(model).where(
                model.actor_type == "agent", (model.actor_id == uuid_array(agent_ids))
            )
        )
    await db.execute(delete(ClientCase).where(ClientCase.agency_id == uuid_array(agency_ids)))
    await db.execute(
        delete(AgentInvitation).where(AgentInvitation.agency_id == uuid_array(agency_ids))
    )
    await db.execute(delete(Agent).where(Agent.agency_id == uuid_array(agency_ids)))
    await db.execute(delete(Agency).where(Agency.id == uuid_array(agency_ids)))
    # Unlike production identities, these clients belong exclusively to the
    # fictional agency (checked above). No storage/provider adapter is called.
    await db.execute(delete(ExpatUser).where(ExpatUser.id == uuid_array(clients)))


async def archived_export(db: AsyncSession, batch: str) -> DemoExportResponse:
    statement = select(history).order_by(history.c.deleted_at.desc())
    if batch:
        statement = statement.where(history.c.batch == batch)
    rows = [
        dict(row.snapshot, deleted_at=row.deleted_at.isoformat())
        for row in (await db.execute(statement)).all()
    ]
    return DemoExportResponse(csv=history_csv(rows), count=len(rows), selection_token="")


async def purge_expired(db: AsyncSession, catalog: Any) -> int:
    from src.demo_sandbox.control_manager import DemoControlManager

    # The transaction lock also protects against two workers running a sweep.
    await db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended('demo-purge', 0))"))
    rows = list(
        await db.scalars(
            select(Agency)
            .where(
                Agency.slug.startswith("demo-sandbox-"),
                expiry_expression() <= datetime.now(UTC) - timedelta(days=30),
            )
            .order_by(Agency.id)
            .limit(100)
            .with_for_update(skip_locked=True)
        )
    )
    manager = DemoControlManager(db, catalog)
    await erase_demos(db, rows, [manager.read(agency) for agency in rows])
    await db.commit()
    return len(rows)

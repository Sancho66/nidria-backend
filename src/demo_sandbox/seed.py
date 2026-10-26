"""Load the approved imports through the real manager, then pose fictional state."""

import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agency import Agency
from shared.models.agent import Agent
from shared.models.case_person import CasePerson
from shared.models.case_step_progress import CaseStepProgress
from shared.models.case_step_requirement import CaseStepRequirement
from shared.models.client_case import ClientCase
from shared.models.document import Document
from shared.models.expat_user import ExpatUser
from shared.models.journey import JourneyTemplateStep
from shared.models.rbac import Role
from shared.models.reminder import Reminder
from shared.models.signature import SignatureRequest, SignatureSigner
from shared.models.step_requirement import StepRequirement
from src.cases.cases_manager import CasesManager
from src.cases.cases_schema import CaseCreateRequest
from src.core.enums import CaseStatus
from src.core.exceptions import ValidationError
from src.core.rbac.baseline import collect_bindings, seed_rbac_baseline
from src.core.security import hash_password
from src.demo_sandbox.bindings import BINDINGS
from src.demo_sandbox.catalog import DemoCatalog, DemoRecord
from src.demo_sandbox.people import fictional_email
from src.demo_sandbox.repository import DemoRepository
from src.demo_sandbox.storage import document_path, load_demo_documents
from src.journeys.import_manager import JourneyImportManager


async def seed_catalog(db: AsyncSession, catalog: DemoCatalog) -> dict[str, int]:
    """Idempotent for a complete seed; refuse unknown or partially seeded data.

    Only the offline seed command may call this batch function. Public reads
    use a SELECT-only role; signed management calls use seed_record separately.
    """
    from sqlalchemy import text

    if await db.scalar(text("SELECT to_regclass('public.demo_history')")) and await db.scalar(
        text("SELECT EXISTS (SELECT 1 FROM demo_history)")
    ):
        raise RuntimeError("Deleted demos must never be restored by the seed command")
    existing = list((await db.scalars(select(Agency))).all())
    if existing:
        if len(existing) != len(catalog.records):
            raise RuntimeError("Refusing a nonempty or partially seeded database")
        for record in catalog.records:
            if await DemoRepository(db).resolve(record, catalog.fingerprint) is None:
                raise RuntimeError("Existing database does not match this demo catalog")
        await seed_rbac_baseline(db, bindings=[*collect_bindings(), *BINDINGS])
        await db.commit()
        return {"created": 0, "already_present": len(catalog.records)}
    await seed_rbac_baseline(db, bindings=[*collect_bindings(), *BINDINGS])
    role = (await db.scalars(select(Role).where(Role.name == "admin", Role.is_system))).one()
    password = hash_password(secrets.token_urlsafe(48))
    for record in catalog.records:
        await seed_record(db, catalog.fingerprint, record, role, password)
    load_demo_documents(catalog)
    return {"created": len(catalog.records), "already_present": 0}


async def seed_record(
    db: AsyncSession,
    fingerprint: str,
    record: DemoRecord,
    role: Role,
    password: str,
    extra_settings: dict[str, Any] | None = None,
    *,
    owner_name: tuple[str, str] | None = None,
    client_name: tuple[str, str] | None = None,
    mail_domain: str | None = None,
) -> Agency:
    agency = Agency(
        name=record.name,
        slug=record.agency_slug,
        default_language=record.language,
        is_internal=True,
        contact_email=f"office@{mail_domain}" if mail_domain else None,
        settings={
            "sandbox_catalog": fingerprint,
            "sandbox_record": record.id,
            "auto_reminders_enabled": False,
            "client_step_notifications_enabled": False,
            **(extra_settings or {}),
        },
    )
    if record.state.get("logo"):
        agency.logo_path = record.state["logo"]["path"]
    db.add(agency)
    await db.flush()
    owner = Agent(
        agency_id=agency.id,
        role_id=role.id,
        first_name=owner_name[0] if owner_name else "Demo" if record.language == "en" else "Démo",
        last_name=owner_name[1] if owner_name else "Team" if record.language == "en" else "Équipe",
        email=fictional_email(owner_name, mail_domain)
        if owner_name and mail_domain
        else f"demo-team-{record.public_code}@example.com",
        password_hash=password,
    )
    db.add(owner)
    await db.commit()
    preview = await JourneyImportManager(db).run(
        owner, record.journey_import["parcours"], preview=True
    )
    if preview.steps_ignored or len(preview.steps_created) != len(record.state["steps"]):
        raise ValidationError(
            "Every demo step must import successfully.", code="demo.invalid_journey"
        )
    imported = await JourneyImportManager(db).run(
        owner, record.journey_import["parcours"], preview=False
    )
    if (
        not imported.created
        or imported.template_id is None
        or len(imported.steps_created) != len(record.state["steps"])
        or imported.steps_ignored
    ):
        raise RuntimeError("Approved journey did not import completely")
    await seed_case(
        db,
        record,
        owner,
        imported.template_id,
        all_requirements=extra_settings is not None,
        first_name=client_name[0] if client_name else None,
        last_name=client_name[1] if client_name else None,
        email=fictional_email(client_name, mail_domain) if client_name and mail_domain else None,
    )
    return agency


async def seed_case(
    db: AsyncSession,
    record: DemoRecord,
    owner: Agent,
    template_id: uuid.UUID,
    *,
    all_requirements: bool = True,
    first_name: str | None = None,
    last_name: str | None = None,
    email: str | None = None,
) -> ClientCase:
    """Reuse the production creation path with all outbound effects disabled."""
    now = datetime.now(UTC)
    case = await CasesManager(db).create_case(
        owner,
        CaseCreateRequest(
            first_name=first_name or ("Demo" if record.language == "en" else "Client"),
            last_name=last_name or ("Client" if record.language == "en" else "Démonstration"),
            email=email or f"demo-client-{record.public_code}@example.com",
            preferred_lang=record.language,
            send_invitation=False,
            journey_template_id=template_id,
            status=CaseStatus.IN_PROGRESS,
        ),
        email_sink=[],
    )
    case.is_demo = True
    expat = await db.get(ExpatUser, case.principal_expat_user_id)
    if expat is None:
        raise RuntimeError("Missing fictional client")
    expat.activated_at = now
    expat.password_hash = None
    person = (
        await db.scalars(
            select(CasePerson).where(CasePerson.case_id == case.id, CasePerson.kind == "principal")
        )
    ).one()
    person.custom_fields = {"demo_project_notes": record.state["notice"]}
    rows = (
        await db.execute(
            select(CaseStepProgress, JourneyTemplateStep)
            .join(JourneyTemplateStep, JourneyTemplateStep.id == CaseStepProgress.template_step_id)
            .where(CaseStepProgress.case_id == case.id)
            .order_by(JourneyTemplateStep.position)
        )
    ).all()
    definitions = list(
        (
            await db.scalars(
                select(StepRequirement).where(
                    StepRequirement.step_id.in_([step.id for _, step in rows])
                )
            )
        ).all()
    )
    provided = [doc for doc in record.state["documents"] if doc["status"] == "validated"]
    documents: dict[tuple[str, str], Document] = {}
    steps_by_ref = {item["ref"]: rows[index][0] for index, item in enumerate(record.state["steps"])}
    signature = record.state["signature"]
    signature_items = [signature] if signature.get("enabled", True) else []
    for position, item in enumerate(provided + signature_items):
        progress = steps_by_ref[item["step_ref"]]
        doc = Document(
            case_id=case.id,
            step_progress_id=progress.id,
            filename=f"{item['label']}.pdf",
            storage_path=document_path(record.id, position),
            uploaded_by_type="system",
            uploaded_by_id=expat.id,
            validation_status="ok",
            kind="deliverable" if position == len(provided) else "deposit",
        )
        db.add(doc)
        await db.flush()
        documents[(item["step_ref"], item["label"])] = doc
    for index, (progress, step) in enumerate(rows):
        progress.status = record.state["steps"][index]["status"]
        if progress.status == "done":
            progress.completed_at = now - timedelta(
                days=max(1, record.state["progress"]["done"] - index)
            )
            progress.completed_by_agent_id = owner.id
        if not all_requirements and index > 2:
            continue
        for definition in [item for item in definitions if item.step_id == step.id]:
            step_ref = record.state["steps"][index]["ref"]
            is_signature = (
                bool(signature_items)
                and step_ref == record.state["signature"]["step_ref"]
                and definition.kind == "document"
                and definition.reference == record.state["signature"]["label"]
            )
            provided_doc = documents.get((step_ref, definition.reference))
            requirement = CaseStepRequirement(
                case_step_progress_id=progress.id,
                step_requirement_id=definition.id,
                person_id=person.id,
                kind=definition.kind,
                reference=definition.reference,
                scope=definition.scope,
                status="provided" if provided_doc else "pending",
                document_id=provided_doc.id if provided_doc else None,
                provided_at=now - timedelta(days=1) if provided_doc else None,
                signature_required=is_signature,
                signature_level="ses",
            )
            db.add(requirement)
            await db.flush()
            if is_signature:
                request = SignatureRequest(
                    case_id=case.id,
                    case_step_progress_id=progress.id,
                    step_requirement_id=definition.id,
                    reference=definition.reference,
                    provider="mock",
                    level="ses",
                    status="completed",
                    completed_at=progress.completed_at or now,
                )
                db.add(request)
                await db.flush()
                db.add(
                    SignatureSigner(
                        signature_request_id=request.id,
                        case_person_id=person.id,
                        case_step_requirement_id=requirement.id,
                        status="signed",
                        signed_at=progress.completed_at or now,
                        details={"fictional": True, "notice": record.state["signature"]["notice"]},
                    )
                )
    for reminder in record.state["reminders"]:
        db.add(
            Reminder(
                case_id=case.id,
                step_progress_id=steps_by_ref[reminder["step_ref"]].id,
                channel="in_app",
                status="sent",
                recipient_type="expat",
                scheduled_at=now - timedelta(days=1),
                message_body=record.state["notice"] + "\n\n" + reminder["body"],
            )
        )
    await db.commit()
    return case

"""Load the approved imports through the real manager, then pose fictional state."""

import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agency import Agency
from shared.models.agent import Agent
from shared.models.case_person import CasePerson
from shared.models.case_step_progress import CaseStepProgress
from shared.models.case_step_requirement import CaseStepRequirement
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
from src.core.rbac.baseline import collect_bindings, seed_rbac_baseline
from src.core.security import hash_password
from src.demo_sandbox.bindings import BINDINGS
from src.demo_sandbox.catalog import DemoCatalog, DemoRecord
from src.demo_sandbox.repository import DemoRepository
from src.demo_sandbox.storage import document_path, load_demo_documents
from src.journeys.import_manager import JourneyImportManager


async def seed_catalog(db: AsyncSession, catalog: DemoCatalog) -> dict[str, int]:
    """Idempotent for a complete seed; refuse unknown or partially seeded data.

    Only the offline seed command may call this function. The serving process
    has SELECT privileges and never imports this module.
    """
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
        await _seed_record(db, catalog, record, role, password)
    load_demo_documents(catalog)
    return {"created": len(catalog.records), "already_present": 0}


async def _seed_record(
    db: AsyncSession, catalog: DemoCatalog, record: DemoRecord, role: Role, password: str
) -> None:
    now = datetime.now(UTC)
    agency = Agency(
        name=record.name,
        slug=record.agency_slug,
        default_language=record.language,
        is_internal=True,
        settings={
            "sandbox_catalog": catalog.fingerprint,
            "sandbox_record": record.id,
            "auto_reminders_enabled": False,
            "client_step_notifications_enabled": False,
        },
    )
    db.add(agency)
    await db.flush()
    owner = Agent(
        agency_id=agency.id,
        role_id=role.id,
        first_name="Demo",
        last_name="Team" if record.language == "en" else "Equipe",
        email=f"demo-team-{record.public_code}@example.com",
        password_hash=password,
    )
    db.add(owner)
    await db.commit()
    imported = await JourneyImportManager(db).run(
        owner, record.journey_import["parcours"], preview=False
    )
    if not imported.created or len(imported.steps_created) != 7 or imported.steps_ignored:
        raise RuntimeError("Approved journey did not import completely")
    case = await CasesManager(db).create_case(
        owner,
        CaseCreateRequest(
            first_name="Demo" if record.language == "en" else "Client",
            last_name="Client" if record.language == "en" else "Demonstration",
            email=f"demo-client-{record.public_code}@example.com",
            preferred_lang=record.language,
            send_invitation=False,
            journey_template_id=imported.template_id,
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
    documents: dict[str, Document] = {}
    for position, item in enumerate(provided + [record.state["signature"]]):
        progress = rows[1][0] if position == len(provided) else rows[2][0]
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
        documents[item["label"]] = doc
    for index, (progress, step) in enumerate(rows):
        progress.status = record.state["steps"][index]["status"]
        if progress.status == "done":
            progress.completed_at = now - timedelta(days=3 - index)
            progress.completed_by_agent_id = owner.id
        if index > 2:
            continue
        for definition in [item for item in definitions if item.step_id == step.id]:
            signature = index == 1 and definition.kind == "document"
            provided_doc = documents.get(definition.reference)
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
                signature_required=signature,
                signature_level="ses",
            )
            db.add(requirement)
            await db.flush()
            if signature:
                request = SignatureRequest(
                    case_id=case.id,
                    case_step_progress_id=progress.id,
                    step_requirement_id=definition.id,
                    reference=definition.reference,
                    provider="mock",
                    level="ses",
                    status="completed",
                    completed_at=progress.completed_at,
                )
                db.add(request)
                await db.flush()
                db.add(
                    SignatureSigner(
                        signature_request_id=request.id,
                        case_person_id=person.id,
                        case_step_requirement_id=requirement.id,
                        status="signed",
                        signed_at=progress.completed_at,
                        details={"fictional": True, "notice": record.state["signature"]["notice"]},
                    )
                )
    for reminder in record.state["reminders"]:
        db.add(
            Reminder(
                case_id=case.id,
                step_progress_id=rows[2][0].id,
                channel="in_app",
                status="sent",
                recipient_type="expat",
                scheduled_at=now - timedelta(days=1),
                message_body=record.state["notice"] + "\n\n" + reminder["body"],
            )
        )
    await db.commit()

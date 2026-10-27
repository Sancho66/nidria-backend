"""Populate the real models with connected fictional agency data, offline only."""

import asyncio
import secrets
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.activity import ActivityLog
from shared.models.agency import Agency
from shared.models.agent import Agent
from shared.models.case_person import CasePerson
from shared.models.case_step_cost import CaseStepCost
from shared.models.case_step_progress import CaseStepProgress
from shared.models.client_case import ClientCase
from shared.models.client_profile import ClientProfile
from shared.models.client_profile_note import ClientProfileNote
from shared.models.company_profile import CompanyProfile, CompanyProfileRole
from shared.models.document_template import DocumentTemplate
from shared.models.expat_user import ExpatUser
from shared.models.external_contact import ExternalContact
from shared.models.journey import JourneyTemplate, JourneyTemplateStep
from shared.models.message_template import MessageTemplate
from shared.models.rbac import Role
from shared.models.reminder import Reminder
from shared.models.saved_view import SavedView
from shared.models.step_comment import StepComment
from shared.models.usage import UsageEvent
from src.admin.demo_schema import AgencyDemoInput
from src.agencies.profile_sections_manager import list_sections
from src.company_profiles.company_catalog import materialize_company_definitions
from src.core.security import hash_password
from src.demo_sandbox.catalog import DemoRecord
from src.demo_sandbox.people import fictional_email, fictional_people
from src.demo_sandbox.seed import seed_case
from src.demo_sandbox.storage import document_path

PROGRESS = (2, 0, 1, 1, 2, 3, 4, 4, 5, 6)


def variant_record(record: DemoRecord, index: int) -> DemoRecord:
    """Stable file paths and progression, reconstructed without storing ten imports."""
    if index == 0:
        return record
    result = record.model_copy(deep=True)
    result.id = f"{record.id}-case-{index}"
    result.state["id"] = result.id
    done = PROGRESS[index]
    result.state["progress"]["done"] = done
    for position, step in enumerate(result.state["steps"]):
        step["status"] = (
            "done" if position < done else "in_progress" if position == done else "todo"
        )
    for item in result.state["documents"]:
        position = next(
            i for i, step in enumerate(result.state["steps"]) if step["ref"] == item["step_ref"]
        )
        item["status"] = "validated" if position < done else "requested"
    result.state["signature"]["enabled"] = done >= 2
    result.state["reminders"] = [] if done == 0 or done == 6 else result.state["reminders"]
    return DemoRecord.model_validate(result.model_dump())


async def populate_agency(
    db: AsyncSession, agency: Agency, record: DemoRecord, payload: AgencyDemoInput, domain: str
) -> dict[str, object]:
    now = datetime.now(UTC)
    fr = payload.language == "fr"
    owner = (await db.scalars(select(Agent).where(Agent.agency_id == agency.id))).one()
    first = (await db.scalars(select(ClientCase).where(ClientCase.agency_id == agency.id))).one()
    assert first.journey_template_id is not None
    people = fictional_people(payload)
    owner.first_name, owner.last_name = (
        (payload.first_name, "") if payload.first_name else people.team[0]
    )
    owner.email = fictional_email(people.team[0], domain)
    journey = await db.get(JourneyTemplate, first.journey_template_id)
    assert journey is not None
    journey.country = payload.country
    agency.country, agency.city = payload.country, payload.city or None
    agency.legal_name = payload.company_name
    agency.legal_form = "SAS" if fr else "LLC" if payload.country == "US" else "Ltd"
    agency.registration_number = "DEMO-000001"
    agency.address = "1 rue de l'Exemple" if fr else "1 Example Street"
    agency.postal_code = "69000" if fr else "00000"
    agency.contact_email = f"office@{domain}"
    agency.contact_phone = "+00 000 000 000"
    agency.currency = {"GB": "GBP", "US": "USD", "CA": "CAD", "AU": "AUD", "NZ": "NZD"}.get(
        payload.country, "EUR"
    )
    agency.onboarding_dismissed_at = now
    agency.client_terms_reviewed_at = now
    agency.settings = {**agency.settings, "auto_reminders_require_approval": True}
    # Production GETs lazily materialize these defaults. Prepare them once
    # in the isolated writer transaction, never during public consultation.
    for surface in ("person", "company"):
        await list_sections(db, agency.id, surface)
    await materialize_company_definitions(db, agency.id)
    password = await asyncio.to_thread(hash_password, secrets.token_urlsafe(48))
    members = [owner]
    roles = {
        name: role_id
        for name, role_id in (
            await db.execute(select(Role.name, Role.id).where(Role.is_system))
        ).all()
    }
    for role_name, (first_name, last_name) in zip(
        ("case_manager", "member"), people.team[1:3], strict=True
    ):
        member = Agent(
            agency_id=agency.id,
            role_id=roles[role_name],
            first_name=first_name,
            last_name=last_name,
            email=fictional_email((first_name, last_name), domain),
            password_hash=password,
        )
        db.add(member)
        members.append(member)
    provider_role = (
        await db.scalars(select(Role).where(Role.name == "external_translator", Role.is_system))
    ).one()
    provider = Agent(
        agency_id=agency.id,
        role_id=provider_role.id,
        first_name=people.team[3][0],
        last_name=people.team[3][1],
        email=fictional_email(people.team[3], domain),
        password_hash=password,
        is_external=True,
    )
    db.add(provider)
    await db.flush()
    db.add(
        ExternalContact(
            agency_id=agency.id,
            agent_id=provider.id,
            name=f"{provider.first_name} {provider.last_name}",
            email=provider.email,
            type="other",
        )
    )
    cases = [first]
    for index in range(1, 10):
        name = people.clients[index]
        cases.append(
            await seed_case(
                db,
                variant_record(record, index),
                owner,
                first.journey_template_id,
                first_name=name[0],
                last_name=name[1],
                email=fictional_email(name, domain),
            )
        )
    profiles = []
    for index, case in enumerate(cases):
        client = await db.get(ExpatUser, case.principal_expat_user_id)
        assert client is not None
        client.first_name, client.last_name = people.clients[index]
        case.owner_agent_id = members[index % 3].id
        case.dest_country = payload.country
        case.dest_city = payload.city or None
        case.reference = f"{now.year}-{index + 1:03}"
        case.tags = ["Exemple fictif" if fr else "Sample data"]
        case.source = "Exemple fictif" if fr else "Sample data"
        if payload.persona in {"couple", "family"}:
            db.add(
                CasePerson(
                    case_id=case.id,
                    kind="family",
                    full_name=f"Alex {client.last_name}",
                    relationship_kind="spouse",
                    relationship="Conjoint" if fr else "Partner",
                )
            )
            if payload.persona == "family":
                db.add(
                    CasePerson(
                        case_id=case.id,
                        kind="family",
                        full_name=f"{'Lou' if fr else 'Sam'} {client.last_name}",
                        relationship_kind="child",
                        relationship="Enfant" if fr else "Child",
                    )
                )
        case.created_at = now - timedelta(days=2 + index * 3)
        case.status = (
            "closed"
            if PROGRESS[index] == 6
            else "awaiting_documents"
            if index in (2, 3, 4)
            else "in_progress"
        )
        profile = (
            await db.scalars(
                select(ClientProfile).where(
                    ClientProfile.agency_id == agency.id, ClientProfile.expat_user_id == client.id
                )
            )
        ).one()
        profile.first_name, profile.last_name, profile.email = (
            client.first_name,
            client.last_name,
            client.email,
        )
        profile.tags = ["Exemple fictif" if fr else "Sample data"]
        profile.source = "Exemple fictif" if fr else "Sample data"
        profile.profession = "Consultant"
        profile.nationality = payload.country
        profile.employer = "Atelier Horizon" if fr else "Willow Studio"
        profile.place_of_birth = payload.city or None
        principal = (
            await db.scalars(
                select(CasePerson).where(
                    CasePerson.case_id == case.id, CasePerson.kind == "principal"
                )
            )
        ).one()
        principal.nationality = profile.nationality
        principal.profession = profile.profession
        principal.employer = profile.employer
        principal.place_of_birth = profile.place_of_birth
        profiles.append(profile)
        db.add(
            ClientProfileNote(
                profile_id=profile.id,
                author_agent_id=owner.id,
                body=(
                    "Données fictives. Le client a confirmé ses priorités "
                    "et les prochaines pièces à transmettre."
                )
                if fr
                else (
                    "Sample data. The client has confirmed their priorities "
                    "and the next documents to provide."
                ),
            )
        )
        rows = list(
            (
                await db.execute(
                    select(CaseStepProgress, JourneyTemplateStep)
                    .join(
                        JourneyTemplateStep,
                        JourneyTemplateStep.id == CaseStepProgress.template_step_id,
                    )
                    .where(CaseStepProgress.case_id == case.id)
                    .order_by(JourneyTemplateStep.position)
                )
            ).all()
        )
        current = rows[min(PROGRESS[index], 5)][0]
        for position, (progress, _step) in enumerate(rows):
            progress.responsible_type = "agent"
            progress.responsible_agent_id = case.owner_agent_id
            progress.validated_by_agent_id = owner.id
            if progress.status == "done":
                progress.completed_by_agent_id = case.owner_agent_id
                progress.completed_at = now - timedelta(days=index % 6, minutes=position + 1)
            if progress.status != "done":
                progress.due_at = now + timedelta(
                    days=-2
                    if index == 3 and progress.id == current.id
                    else max(0, position - PROGRESS[index]) + index % 5 + 1
                )
        db.add(
            ActivityLog(
                case_id=case.id,
                actor_type="agent",
                actor_id=case.owner_agent_id,
                action_type="case.status_changed" if PROGRESS[index] == 6 else "document.validated",
                details={"fictional": True, "new": case.status},
                created_at=now - timedelta(days=index % 6, minutes=index + 1),
            )
        )
        db.add(
            StepComment(
                case_step_progress_id=current.id,
                author_type="agent",
                author_id=owner.id,
                body=(
                    "Nous avons vérifié les premières pièces. "
                    "La liste des éléments restants est à jour."
                )
                if fr
                else (
                    "We have checked the first documents. "
                    "The list of outstanding items is up to date."
                ),
            )
        )
        db.add(
            StepComment(
                case_step_progress_id=current.id,
                author_type="expat",
                author_id=client.id,
                body="Merci, je vais préparer les documents restants."
                if fr
                else "Thank you, I will prepare the remaining documents.",
            )
        )
        db.add(
            CaseStepCost(
                case_step_progress_id=current.id,
                currency=agency.currency,
                planned_currency=agency.currency,
                planned_amount=Decimal("45.00"),
                amount=Decimal("45.00") if index % 2 else None,
                label="Frais de traduction, exemple" if fr else "Translation fee, sample",
                author_agent_id=owner.id,
            )
        )
        if PROGRESS[index] < 6:
            db.add(
                Reminder(
                    case_id=case.id,
                    step_progress_id=current.id,
                    channel="mail",
                    recipient_type="expat",
                    status="to_approve" if index % 2 else "approved",
                    scheduled_at=now + timedelta(days=index % 5 + 1),
                    message_body=(
                        "Exemple fictif. Les pièces restantes sont indiquées "
                        "dans votre espace client."
                    )
                    if fr
                    else "Sample message. Your client space lists the documents still needed.",
                )
            )
        db.add(
            UsageEvent(
                agency_id=agency.id,
                case_id=case.id,
                actor_type="expat",
                actor_id=client.id,
                event_type="document.added",
                details={"fictional": True},
                created_at=now - timedelta(hours=index + 1),
            )
        )
        if index < 3:
            company = CompanyProfile(
                agency_id=agency.id,
                name=(f"Atelier Horizon {index + 1}" if fr else f"Willow Studio {index + 1}"),
                source="Exemple fictif" if fr else "Sample data",
                tags=["Exemple fictif" if fr else "Sample data"],
            )
            db.add(company)
            await db.flush()
            case.company_profile_id = company.id
            db.add(
                CompanyProfileRole(
                    company_profile_id=company.id, client_profile_id=profile.id, role="manager"
                )
            )
    db.add(
        SavedView(
            agency_id=agency.id,
            agent_id=owner.id,
            name="Pièces en attente" if fr else "Awaiting documents",
            entity="cases",
            filters={
                "advanced_filters": [
                    {"field": "status", "operator": "eq", "value": "awaiting_documents"}
                ]
            },
            is_shared=True,
        )
    )
    db.add(
        MessageTemplate(
            agency_id=agency.id,
            name="Pièces manquantes" if fr else "Missing documents",
            language=payload.language,
            channel="mail",
            body="Bonjour {client_name}, les pièces à transmettre sont indiquées dans votre espace."
            if fr
            else "Hello {client_name}, your client space lists the documents we still need.",
        )
    )
    db.add(
        DocumentTemplate(
            agency_id=agency.id,
            name="Lettre de mission, exemple" if fr else "Engagement letter, sample",
            storage_path=document_path(
                record.id, sum(doc["status"] == "validated" for doc in record.state["documents"])
            ),
            filename="engagement-letter-sample.pdf",
            provider="mock",
            provider_template_ref=f"fictional-{record.public_code}",
            fields_configured=True,
            roles_count=1,
            state="active",
        )
    )
    await db.commit()
    return {
        "agent_id": str(owner.id),
        "case_ids": [str(case.id) for case in cases],
        "email": str(payload.email),
        "country": payload.country,
        "profession": payload.profession,
        "persona": payload.persona,
        "steps": payload.steps,
        "step_titles": payload.step_titles,
        "journey_name": payload.journey_name,
        "documents": payload.documents,
        "expires_at": (now + timedelta(days=60)).isoformat(),
    }

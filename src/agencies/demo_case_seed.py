"""Sample dossier seeded at agency activation (nurture bloc 2).

Eric's decision (2026-07-03): ONE generalist example case, IDENTICAL for
every agency, cloned automatically at agency creation with its info page
already filled — so the S0 J+7 nurture email ("you already have an
example in your space") is TRUE.

What the agency gets:
- a generalist 5-step journey template, owned by the agency (a normal,
  reusable template — Eric's call: the journey is a GIFT, only the CASE
  is demo). Deliberately NO `journey.created` event: a system seed is
  not an agency action, `premier_parcours_cree` keeps its meaning.
- a demo client (`Client Exemple`, demo+<slug>@nidria.app) with a
  SIMULATED activation: `activated_at` set (the "account active" badge
  is visually true, impersonation works), throwaway random password,
  and NO email can ever reach it (suppressed at the send_email sink).
- the case itself, `is_demo=TRUE` → bloc 1 excludes it from EVERY usage
  signal (events, milestones, backfill, counters): S0 stays S0.
- a lived-in timeline: 2 steps DONE, 1 IN_PROGRESS, 2 TODO (chained
  prerequisites), civil info filled, 1 sample document, 1 client comment.

Idempotence marker: `agency.settings["demo_case_seeded_at"]` — poses at
first seed and SURVIVES the case's deletion, so nothing ever re-creates
the example behind the agency's back (deleting it is a valid choice)."""

import asyncio
import logging
import uuid
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agency import Agency
from shared.models.agent import Agent
from shared.models.case_person import CasePerson
from shared.models.case_step_progress import CaseStepProgress
from shared.models.client_case import ClientCase
from shared.models.document import Document
from shared.models.expat_user import ExpatUser
from shared.models.journey import JourneyTemplate, JourneyTemplateStep, StepPrerequisite
from shared.models.step_comment import StepComment
from shared.models.step_requirement import StepRequirement
from src.core import storage
from src.core.email import demo_expat_email
from src.core.enums import ActorType, CaseStatus, DocValidationStatus, StepStatus
from src.core.security import hash_password

logger = logging.getLogger(__name__)

DEMO_SEED_MARKER = "demo_case_seeded_at"

DEMO_JOURNEY_NAME = "Exemple — Installation à l'étranger"

# (name, estimated_days, content_note, status) — linear AND chain, the
# agency validates every step (template defaults). Generic on purpose:
# the same example must speak to a Paraguay agency and a Cyprus one.
_DEMO_STEPS: list[tuple[str, int | None, str, str]] = [
    (
        "Premier rendez-vous & recueil des informations",
        7,
        "Échange initial avec le client : situation, objectifs, calendrier. "
        "Les informations recueillies alimentent la page d'infos du dossier.",
        StepStatus.DONE.value,
    ),
    (
        "Constitution du dossier & pièces justificatives",
        14,
        "Le client dépose ses pièces directement dans son espace : chaque "
        "document arrive au bon endroit, plus rien ne se perd dans les mails.",
        StepStatus.DONE.value,
    ),
    (
        "Dépôt de la demande auprès de l'administration",
        30,
        "L'agence dépose le dossier complet. Le client suit l'avancement en "
        "temps réel depuis son espace — sans avoir besoin d'appeler.",
        StepStatus.IN_PROGRESS.value,
    ),
    (
        "Réception de la décision & titre de résidence",
        21,
        "Dès la décision reçue, l'étape est validée et le client est prévenu automatiquement.",
        StepStatus.TODO.value,
    ),
    (
        "Installation & suivi sur place",
        None,
        "Dernière ligne droite : installation, démarches locales et suivi par votre équipe.",
        StepStatus.TODO.value,
    ),
]

# Document requirements shown on step 2 (what the client is asked for).
_DEMO_REQUIREMENTS = ("Copie du passeport", "Justificatif de domicile")

_DEMO_COMMENT = (
    "Bonjour ! Je viens de déposer mes pièces justificatives — "
    "dites-moi s'il manque quelque chose. Merci pour le suivi !"
)

_DEMO_DOCUMENT_FILENAME = "passeport-client-exemple.pdf"

# A tiny but VALID one-page PDF (opens in any viewer): the example
# document must survive a real download click.
_DEMO_DOCUMENT_PDF = (
    b"%PDF-1.4\n"
    b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 595 842]/Resources"
    b"<</Font<</F1 4 0 R>>>>/Contents 5 0 R>>endobj\n"
    b"4 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj\n"
    b"5 0 obj<</Length 96>>stream\n"
    b"BT /F1 18 Tf 72 770 Td (Document d'exemple - Nidria) Tj ET\n"
    b"BT /F1 11 Tf 72 745 Td (Piece factice du dossier de demonstration.) Tj ET\n"
    b"endstream endobj\n"
    b"trailer<</Root 1 0 R>>\n"
    b"%%EOF\n"
)


def _demo_settings(agency: Agency, now: datetime) -> dict[str, object]:
    # JSONB: reassign a NEW dict so SQLAlchemy sees the mutation.
    return {**agency.settings, DEMO_SEED_MARKER: now.isoformat()}


async def seed_demo_case(db: AsyncSession, agency: Agency, owner: Agent) -> ClientCase | None:
    """Create the example dossier for `agency`, owned by `owner` (its
    first admin at creation; the earliest member for backfills). COMMITS.

    Idempotent by the settings marker — once seeded (even if the agency
    later deletes the case), calling again is a no-op returning None.
    Emits NO usage event, sends NO email, logs NO activity: the example
    must be invisible to every adoption signal."""
    if agency.settings.get(DEMO_SEED_MARKER):
        return None
    now = datetime.now(UTC)

    # --- the demo client, activation SIMULATED (badge true, no email ever) ---------
    email = demo_expat_email(agency.slug)
    expat = (
        await db.execute(select(ExpatUser).where(ExpatUser.email == email))
    ).scalar_one_or_none()
    if expat is None:
        expat = ExpatUser(
            first_name="Client",
            last_name="Exemple",
            email=email,
            preferred_lang=agency.default_language,
            # Throwaway: nobody ever logs in as the demo client directly
            # (the agency uses "voir comme le client" / impersonation).
            password_hash=hash_password(uuid.uuid4().hex + uuid.uuid4().hex),
            activated_at=now - timedelta(days=15),
        )
        db.add(expat)
        await db.flush()

    # --- the journey: a NORMAL agency template (reusable gift) ---------------------
    template = JourneyTemplate(agency_id=agency.id, name=DEMO_JOURNEY_NAME)
    db.add(template)
    await db.flush()
    steps: list[JourneyTemplateStep] = []
    for position, (name, days, note, _status) in enumerate(_DEMO_STEPS):
        step = JourneyTemplateStep(
            template_id=template.id,
            name=name,
            position=position,
            estimated_days=days,
            content_note=note,
            default_validated_by_type="agent",
        )
        steps.append(step)
        db.add(step)
    await db.flush()
    for i in range(1, len(steps)):
        db.add(StepPrerequisite(step_id=steps[i].id, prerequisite_step_id=steps[i - 1].id))
    for position, label in enumerate(_DEMO_REQUIREMENTS):
        db.add(
            StepRequirement(
                step_id=steps[1].id,
                kind="document",
                reference=label,
                scope="principal",
                position=position,
            )
        )

    # --- the case itself: is_demo=TRUE is THE exclusion switch ---------------------
    case = ClientCase(
        agency_id=agency.id,
        principal_expat_user_id=expat.id,
        owner_agent_id=owner.id,
        journey_template_id=template.id,
        origin_country="FR",
        origin_city="Lyon",
        dest_country="PT",
        dest_city="Lisbonne",
        status=CaseStatus.IN_PROGRESS.value,
        source="Dossier d'exemple",
        tags=["exemple"],
        is_demo=True,
        created_at=now - timedelta(days=21),
    )
    db.add(case)
    await db.flush()
    db.add(
        CasePerson(
            case_id=case.id,
            kind="principal",
            expat_user_id=expat.id,
            nationality="Française",
            date_of_birth=date(1988, 5, 14),
            place_of_birth="Lyon, France",
            phone="+33 6 12 34 56 78",
            profession="Consultante indépendante",
            custom_fields={},
        )
    )

    # --- a lived-in timeline: 2 DONE, 1 IN_PROGRESS, 2 TODO ------------------------
    progresses: list[CaseStepProgress] = []
    for i, ((_name, _days, _note, status), step) in enumerate(zip(_DEMO_STEPS, steps, strict=True)):
        done = status == StepStatus.DONE.value
        progress = CaseStepProgress(
            case_id=case.id,
            template_step_id=step.id,
            status=status,
            # Client steps (2 and 5) belong to the client; the agency
            # owner carries the others — real interlocutors on the
            # timeline. CHECK: type 'agent' requires the agent FK.
            responsible_type="expat" if i in (1, 4) else "agent",
            responsible_agent_id=None if i in (1, 4) else owner.id,
            validated_by_type="agent",
            completed_at=(now - timedelta(days=20 - 4 * i)) if done else None,
            completed_by_agent_id=owner.id if done else None,
        )
        progresses.append(progress)
        db.add(progress)
    await db.flush()

    # --- one sample document on the done "pièces" step ------------------------------
    document_id = uuid.uuid4()
    path = f"{case.id}/{document_id}/{storage.sanitize_filename(_DEMO_DOCUMENT_FILENAME)}"
    await asyncio.to_thread(storage.upload, path, _DEMO_DOCUMENT_PDF, "application/pdf")
    db.add(
        Document(
            id=document_id,
            case_id=case.id,
            step_progress_id=progresses[1].id,
            filename=_DEMO_DOCUMENT_FILENAME,
            storage_path=path,
            uploaded_by_type=ActorType.EXPAT.value,
            uploaded_by_id=expat.id,
            validation_status=DocValidationStatus.OK.value,
            created_at=now - timedelta(days=17),
        )
    )

    # --- one client message on the live step (the thread feels real) ----------------
    db.add(
        StepComment(
            case_step_progress_id=progresses[2].id,
            author_type=ActorType.EXPAT.value,
            author_id=expat.id,
            body=_DEMO_COMMENT,
            created_at=now - timedelta(days=2),
        )
    )

    agency.settings = _demo_settings(agency, now)
    await db.commit()
    logger.info("demo case seeded for agency %s (case %s)", agency.slug, case.id)
    return case

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
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agency import Agency
from shared.models.agent import Agent
from shared.models.case_person import CasePerson
from shared.models.case_step_progress import CaseStepProgress
from shared.models.client_case import ClientCase
from shared.models.custom_field import CustomFieldDefinition
from shared.models.document import Document
from shared.models.expat_user import ExpatUser
from shared.models.journey import (
    JourneySection,
    JourneyStepParticipant,
    JourneyTemplate,
    JourneyTemplateField,
    JourneyTemplateStep,
    StepPrerequisite,
)
from shared.models.step_comment import StepComment
from shared.models.step_requirement import StepRequirement
from src.core import storage
from src.core.email import demo_expat_email
from src.core.enums import ActorType, CaseStatus, DocValidationStatus, StepStatus
from src.core.security import hash_password
from src.journeys.field_catalog import FIELD_PRESETS, field_kind

logger = logging.getLogger(__name__)

DEMO_SEED_MARKER = "demo_case_seeded_at"

# Legacy name of the pre-sector demo journey. NO LONGER CREATED (the demo
# case now rides the FIRST cloned sector journey). Kept ONLY as the adoption-
# signal discriminant for agencies seeded BEFORE the sector library — their
# "Exemple : …" journey stays excluded from `premier_parcours_cree` exactly as
# before (the zero-impact invariant). Paired with `sector IS NULL` for the new
# gifted journeys. See admin_repository / agencies_manager.
DEMO_JOURNEY_NAME = "Exemple : Installation à l'étranger"

_DEMO_COMMENT = (
    "Bonjour ! Je viens de déposer mes pièces justificatives, "
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


async def _clone_sector_into_agency(
    db: AsyncSession, src: JourneyTemplate, agency: Agency
) -> tuple[JourneyTemplate, list[JourneyTemplateStep]]:
    """Deep-copy a GLOBAL sector template into `agency` as a REAL reusable
    journey (the gift). Keeps `sector` (provenance + the adoption-signal
    discriminant); is_sample=False. Emits NO activity and does NOT commit —
    it runs inside seed_demo_case's transaction, invisible to every signal.

    Sector templates carry ONLY agent/expat participants (agent_id/external_id
    NULL — résolution A), so copying them verbatim leaks no cross-agency FK."""
    new_tpl = JourneyTemplate(
        id=uuid.uuid4(),
        agency_id=agency.id,
        is_sample=False,
        sector=src.sector,
        name=src.name,
        name_i18n=dict(src.name_i18n or {}),
    )
    db.add(new_tpl)
    await db.flush()

    src_steps = (
        (
            await db.execute(
                select(JourneyTemplateStep)
                .where(JourneyTemplateStep.template_id == src.id)
                .order_by(JourneyTemplateStep.position)
            )
        )
        .scalars()
        .all()
    )
    id_map: dict[uuid.UUID, uuid.UUID] = {}
    new_steps: list[JourneyTemplateStep] = []
    for src_step in src_steps:
        nid = uuid.uuid4()
        id_map[src_step.id] = nid
        step = JourneyTemplateStep(
            id=nid,
            template_id=new_tpl.id,
            name=src_step.name,
            position=src_step.position,
            estimated_days=src_step.estimated_days,
            content_note=src_step.content_note,
            completion_mode=src_step.completion_mode,
            default_validated_by_type=src_step.default_validated_by_type,
        )
        new_steps.append(step)
        db.add(step)
    await db.flush()

    src_ids = list(id_map.keys())
    prereqs = (
        (await db.execute(select(StepPrerequisite).where(StepPrerequisite.step_id.in_(src_ids))))
        .scalars()
        .all()
    )
    for prereq in prereqs:
        db.add(
            StepPrerequisite(
                step_id=id_map[prereq.step_id],
                prerequisite_step_id=id_map[prereq.prerequisite_step_id],
            )
        )
    requirements = (
        (await db.execute(select(StepRequirement).where(StepRequirement.step_id.in_(src_ids))))
        .scalars()
        .all()
    )
    for req in requirements:
        db.add(
            StepRequirement(
                step_id=id_map[req.step_id],
                kind=req.kind,
                reference=req.reference,
                scope=req.scope,
                position=req.position,
            )
        )
    participants = (
        (
            await db.execute(
                select(JourneyStepParticipant).where(JourneyStepParticipant.step_id.in_(src_ids))
            )
        )
        .scalars()
        .all()
    )
    for part in participants:
        db.add(
            JourneyStepParticipant(
                step_id=id_map[part.step_id],
                type=part.type,
                agent_id=part.agent_id,
                external_id=part.external_id,
                role=part.role,
            )
        )

    # --- "Informations du dossier" sections + fields (the sector field pack) ---------
    # Copied like the steps (snapshot): the agency edits its clone freely.
    src_sections = (
        (await db.execute(select(JourneySection).where(JourneySection.template_id == src.id)))
        .scalars()
        .all()
    )
    section_map: dict[uuid.UUID, uuid.UUID] = {}
    for sec in src_sections:
        nid = uuid.uuid4()
        section_map[sec.id] = nid
        db.add(
            JourneySection(
                id=nid,
                template_id=new_tpl.id,
                name=sec.name,
                description=sec.description,
                name_i18n=dict(sec.name_i18n),
                description_i18n=dict(sec.description_i18n),
                seed_key=sec.seed_key,
                position=sec.position,
            )
        )
    src_fields = (
        (
            await db.execute(
                select(JourneyTemplateField).where(JourneyTemplateField.template_id == src.id)
            )
        )
        .scalars()
        .all()
    )
    for fld in src_fields:
        db.add(
            JourneyTemplateField(
                template_id=new_tpl.id,
                kind=fld.kind,
                reference=fld.reference,
                position=fld.position,
                required_at_creation=fld.required_at_creation,
                section_id=section_map.get(fld.section_id) if fld.section_id else None,
            )
        )
    # The catalogue fields reference custom keys with NO definition in this
    # agency (the source is agency-less) — materialize the missing ones so the
    # clone renders resolved, never orphaned (same as clone_template).
    await _materialize_field_definitions(db, agency, src_fields)
    return new_tpl, new_steps


async def _materialize_field_definitions(
    db: AsyncSession, agency: Agency, fields: Sequence[JourneyTemplateField]
) -> None:
    """Create the agency's custom_field_definition rows for the catalogue keys
    referenced by the copied fields and absent from the agency (any state).
    Base fields (not in FIELD_PRESETS) need no definition. Label / options in
    the agency language, full label_i18n. Mirror of clone_template's
    _materialize_catalog_definitions, side-effect-free (no event, no commit)."""
    wanted = {
        f.reference
        for f in fields
        if f.reference in FIELD_PRESETS and field_kind(f.reference) == "custom_field"
    }
    if not wanted:
        return
    existing = {
        d.key
        for d in (
            await db.execute(
                select(CustomFieldDefinition).where(CustomFieldDefinition.agency_id == agency.id)
            )
        ).scalars()
    }
    lang = agency.default_language
    for key in sorted(wanted - existing):
        preset = FIELD_PRESETS[key]
        options = None
        if preset.options is not None:
            options = preset.options.get(lang) or preset.options["fr"]
        db.add(
            CustomFieldDefinition(
                agency_id=agency.id,
                key=key,
                label=preset.labels.get(lang) or preset.labels["fr"],
                label_i18n=dict(preset.labels),
                field_type=preset.field_type,
                options=options,
            )
        )


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

    # --- the gift: clone each checked sector's library journey into the agency -----
    # Real reusable journeys (sector kept = provenance + adoption-signal
    # discriminant), NOT counted as agency-created (see admin_repository).
    cloned: list[tuple[JourneyTemplate, list[JourneyTemplateStep]]] = []
    for sector in agency.sectors:
        src = (
            await db.execute(
                select(JourneyTemplate).where(
                    JourneyTemplate.agency_id.is_(None),
                    JourneyTemplate.is_sample.is_(True),
                    JourneyTemplate.sector == sector,
                )
            )
        ).scalar_one_or_none()
        if src is None:
            continue  # a sector with no library template → 0 journey, no error
        cloned.append(await _clone_sector_into_agency(db, src, agency))

    if not cloned:
        # No sector matched (defensive: the 7 all exist, sectors is mandatory
        # at creation). Mark so nothing retries behind the agency's back; no
        # demo case without a journey to ride.
        agency.settings = _demo_settings(agency, now)
        await db.commit()
        return None

    # The demo case rides the FIRST cloned sector journey.
    demo_template, demo_steps = cloned[0]
    expat_step_ids = set(
        (
            await db.execute(
                select(JourneyStepParticipant.step_id).where(
                    JourneyStepParticipant.step_id.in_([s.id for s in demo_steps]),
                    JourneyStepParticipant.type == "expat",
                )
            )
        )
        .scalars()
        .all()
    )

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

    # --- the case itself: is_demo=TRUE is THE exclusion switch ---------------------
    case = ClientCase(
        agency_id=agency.id,
        principal_expat_user_id=expat.id,
        owner_agent_id=owner.id,
        journey_template_id=demo_template.id,
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

    # --- a lived-in timeline: first 2 DONE, 3rd IN_PROGRESS, rest TODO -------------
    # Responsible mirrors the cloned journey's participants: a step with an
    # expat doer → the client carries it (CHECK: type 'expat' ⇒ no agent FK);
    # otherwise the agency owner (a step with no participant included).
    progresses: list[CaseStepProgress] = []
    for i, step in enumerate(demo_steps):
        if i < 2:
            status = StepStatus.DONE.value
        elif i == 2:
            status = StepStatus.IN_PROGRESS.value
        else:
            status = StepStatus.TODO.value
        done = status == StepStatus.DONE.value
        is_expat = step.id in expat_step_ids
        progress = CaseStepProgress(
            case_id=case.id,
            template_step_id=step.id,
            status=status,
            responsible_type="expat" if is_expat else "agent",
            responsible_agent_id=None if is_expat else owner.id,
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

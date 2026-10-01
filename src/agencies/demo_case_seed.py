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
from typing import Any

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
from src.client_profiles.profile_sections import catalog_classification
from src.core import storage
from src.core.email import demo_expat_email
from src.core.enums import ActorType, CaseStatus, DocValidationStatus, StepStatus
from src.core.i18n import resolve_i18n
from src.core.security import hash_password
from src.journeys.field_catalog import FIELD_PRESETS, field_kind
from src.journeys.sector_seed import sector_doc_label
from src.journeys.sector_seed_i18n import DEMO_COMMON_I18N, DEMO_I18N, EXAMPLE_PREFIX_I18N

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


# Per-sector demo dossier: the example client's last name (first name is the
# universal "[Exemple]" tag) + the pre-filled section custom_fields. Values are
# RAW coercer forms (select = exact option, date = "YYYY-MM-DD", number = int,
# country = ISO-2, address = {street,city,postal_code,country} subset) and only
# reference slugs that EXIST in the sector's sections (the definitions are
# materialized by _clone_sector_into_agency, so the values render). Sector-
# neutral / international (PT, DE) by design. NO em/en dash (dash guard).
_DEMO_BY_SECTOR: dict[str, dict[str, Any]] = {
    "real_estate": {
        "last_name": "Bien immobilier - Residence Horizon",
        "custom_fields": {
            "property_deal_type": "Vente",
            "property_kind": "Appartement",
            "property_address": {
                "street": "12 Ocean View Avenue",
                "city": "Lisboa",
                "postal_code": "1990-095",
                "country": "PT",
            },
            "property_surface": 78,
            "property_rooms": 3,
            "property_price": 320000,
            "mandate_type": "Exclusif",
            "mandate_number": "MND-2026-014",
            "transaction_stage": "Visites",
            "energy_performance": "B",
            "expected_signing_date": "2026-05-15",
            "agency_fee_percent": 4,
        },
    },
    "wealth": {
        "last_name": "Client patrimonial - A. Meyer",
        "custom_fields": {
            "risk_profile": "Équilibré",
            "wealth_objective": "Transmission",
            "investment_horizon": "Long terme",
            "estimated_wealth": 850000,
            "savings_capacity": 2500,
            "funds_origin": "Cession",
            "held_products": "Assurance-vie, comptes-titres, immobilier locatif",
            "esg_preference": "Oui",
        },
    },
    "consulting": {
        "last_name": "Client conseil - Nord Digital",
        "custom_fields": {
            "mission_type": "Diagnostic",
            "billing_mode": "Forfait",
            "daily_rate": 850,
            "sold_budget": 42000,
            "sold_days": 45,
            "completion_rate": 35,
            "consumed_days": 16,
            "expected_deliverables": (
                "Rapport de diagnostic, feuille de route priorisée, restitution comité de pilotage"
            ),
            "steering_committee_freq": "Bi-mensuel",
        },
    },
    "legal": {
        "last_name": "Dossier contentieux - Voss / Delta Trading",
        "custom_fields": {
            "legal_form": "Private limited company",
            "company_registration_number": "HRB 128455",
            "company_name": "Delta Trading Ltd",
        },
    },
    "accounting": {
        # fiscal_year_end OMITTED: no such catalog slug in the tax section.
        "last_name": "Client comptable - Brightwave GmbH",
        "custom_fields": {
            "legal_form": "Limited company",
            "company_registration_number": "DE 811 405 folder",
            "company_name": "Brightwave GmbH",
            "tax_id": "DE299872249",
        },
    },
    "hr_mobility": {
        # "position" -> job_title (the real professional slug). mobility_type
        # OMITTED (no such slug). origin/dest are NATIVE case columns.
        "last_name": "Salarié en mobilité - J. Almeida",
        "origin_country": "PT",
        "dest_country": "DE",
        "custom_fields": {
            "job_title": "Ingénieur logiciel senior",
        },
    },
    "immigration": {
        # Mapped onto REAL immigration slugs, values = REAL select options:
        # "Permis de travail"/motif "Travail" -> visa_type "Travail";
        # "Premiere demande" -> immigration_status "En cours de demande";
        # "foreign_id_number" -> residence_permit_number. competent_authority
        # OMITTED (no slug).
        "last_name": "Demandeur - S. Okoro",
        "custom_fields": {
            "visa_type": "Travail",
            "immigration_status": "En cours de demande",
            "residence_permit_number": "X-2026-55810",
        },
    },
}


def _demo_settings(agency: Agency, now: datetime) -> dict[str, object]:
    # JSONB: reassign a NEW dict so SQLAlchemy sees the mutation.
    return {**agency.settings, DEMO_SEED_MARKER: now.isoformat()}


def _demo_text(table: dict[str, str], lang: str) -> str:
    """A demo label in the agency language, FR when it has no variant."""
    return table.get(lang) or table["fr"]


def _localized_demo_fields(sector: str, raw: dict[str, Any], lang: str) -> dict[str, Any]:
    """The demo custom_fields in the agency language. A select stores the
    OPTION text, and the agency's definitions are materialized with the
    options of ITS language: a FR value (« Vente ») was no option at all of
    an English agency's field. The option at the same position is taken;
    a free-text value uses its translation; numbers, dates, addresses and
    identifiers stay as they are."""
    texts = DEMO_I18N.get(sector, {}).get("text_fields") or {}
    out: dict[str, Any] = {}
    for key, value in raw.items():
        preset = FIELD_PRESETS.get(key)
        fr_options = (preset.options or {}).get("fr") if preset and preset.options else None
        lang_options = (preset.options or {}).get(lang) if preset and preset.options else None
        if fr_options and lang_options and len(fr_options) == len(lang_options):
            if isinstance(value, str) and value in fr_options:
                value = lang_options[fr_options.index(value)]
            elif isinstance(value, list):
                value = [lang_options[fr_options.index(v)] if v in fr_options else v for v in value]
        elif key in texts and isinstance(value, str):
            value = texts[key].get(lang) or value
        out[key] = value
    return out


async def _clone_sector_into_agency(
    db: AsyncSession, src: JourneyTemplate, agency: Agency
) -> tuple[JourneyTemplate, list[JourneyTemplateStep]]:
    """Deep-copy a GLOBAL sector template into `agency` as a REAL reusable
    journey (the gift). Keeps `sector` (provenance + the adoption-signal
    discriminant); is_sample=False. Emits NO activity and does NOT commit —
    it runs inside seed_demo_case's transaction, invisible to every signal.

    Sector templates carry ONLY agent/expat participants (agent_id/external_id
    NULL — résolution A), so copying them verbatim leaks no cross-agency FK."""
    lang = agency.default_language or "fr"
    new_tpl = JourneyTemplate(
        id=uuid.uuid4(),
        agency_id=agency.id,
        is_sample=False,
        sector=src.sector,
        origin="seed",  # un cadeau système, jamais un geste d'agence
        # The agency reads its gift in ITS language: the scalar is the
        # agency-language variant (as clone_template does), the blob is kept.
        name=resolve_i18n(src.name_i18n, lang, lang, src.name) or src.name,
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
            name=resolve_i18n(src_step.name_i18n, lang, lang, src_step.name) or src_step.name,
            name_i18n=dict(src_step.name_i18n or {}),
            position=src_step.position,
            estimated_days=src_step.estimated_days,
            content_note=resolve_i18n(
                src_step.content_note_i18n, lang, lang, src_step.content_note
            ),
            content_note_i18n=dict(src_step.content_note_i18n or {}),
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
    position_of = {s.id: s.position for s in src_steps}
    for req in requirements:
        reference = req.reference
        if req.kind == "document" and src.sector:
            # A document label is free text (no i18n blob): the agency's copy
            # gets it in the agency language.
            reference = sector_doc_label(src.sector, position_of[req.step_id], reference, lang)
        db.add(
            StepRequirement(
                step_id=id_map[req.step_id],
                kind=req.kind,
                reference=reference,
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
        # LA CLASSIFICATION DU CATALOGUE, qui manquait ici : ces
        # définitions naissaient sur les défauts de colonne ('case' /
        # 'misc'), donc invisibles sur les fiches clients — y compris pour
        # des clés que le catalogue classe « personne ». Toute agence
        # créée depuis le 04/08 en a hérité (constat du 06/08, agence
        # QuinnAshford : 22 définitions, toutes en 'case'/'misc').
        scope, section = catalog_classification(key)
        db.add(
            CustomFieldDefinition(
                agency_id=agency.id,
                key=key,
                label=preset.labels.get(lang) or preset.labels["fr"],
                label_i18n=dict(preset.labels),
                field_type=preset.field_type,
                options=options,
                scope=scope,
                profile_section=section,
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
    # One demo dossier PER cloned sector journey (supersedes Eric's single
    # generalist example, 2026-07-03): each rides its own journey and pre-
    # fills that sector's section fields.
    first_case: ClientCase | None = None
    for index, (template, steps) in enumerate(cloned, start=1):
        spec = _DEMO_BY_SECTOR.get(template.sector or "", {})
        lang = agency.default_language or "fr"
        custom_fields: dict[str, Any] = _localized_demo_fields(
            template.sector or "", dict(spec.get("custom_fields", {})), lang
        )
        last_name_i18n = DEMO_I18N.get(template.sector or "", {}).get("last_name") or {}
        expat_step_ids = set(
            (
                await db.execute(
                    select(JourneyStepParticipant.step_id).where(
                        JourneyStepParticipant.step_id.in_([s.id for s in steps]),
                        JourneyStepParticipant.type == "expat",
                    )
                )
            )
            .scalars()
            .all()
        )

        # --- the demo client, activation SIMULATED (badge true, no email ever) -----
        email = demo_expat_email(agency.slug, index)
        expat = (
            await db.execute(select(ExpatUser).where(ExpatUser.email == email))
        ).scalar_one_or_none()
        if expat is None:
            expat = ExpatUser(
                first_name=(EXAMPLE_PREFIX_I18N.get(lang) or "[Exemple]").strip(),
                last_name=str(last_name_i18n.get(lang) or spec.get("last_name", "Dossier exemple")),
                email=email,
                preferred_lang=agency.default_language,
                # Throwaway: nobody logs in as the demo client (impersonation).
                password_hash=hash_password(uuid.uuid4().hex + uuid.uuid4().hex),
                activated_at=now - timedelta(days=15),
            )
            db.add(expat)
            await db.flush()

        # --- the case: is_demo=TRUE is THE exclusion switch, tags=["exemple"] -------
        case = ClientCase(
            agency_id=agency.id,
            principal_expat_user_id=expat.id,
            owner_agent_id=owner.id,
            journey_template_id=template.id,
            origin_country=str(spec.get("origin_country", "PT")),
            dest_country=str(spec.get("dest_country", "DE")),
            status=CaseStatus.IN_PROGRESS.value,
            source=_demo_text(DEMO_COMMON_I18N["source"], lang),
            tags=[_demo_text(DEMO_COMMON_I18N["tag"], lang)],
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
                # A COUNTRY field (ISO code), as the forms and the PDF read it:
                # « Portugaise » was a French word no country lookup knew.
                nationality="PT",
                date_of_birth=date(1988, 5, 14),
                place_of_birth="Porto",
                phone="+351 912 345 678",
                profession=_demo_text(DEMO_COMMON_I18N["profession"], lang),
                # Pre-filled sector fields (raw coercer forms; definitions already
                # materialized by _clone_sector_into_agency → the values render).
                custom_fields=custom_fields,
            )
        )

        # --- lived-in timeline: first 2 DONE, 3rd IN_PROGRESS, rest TODO -----------
        progresses: list[CaseStepProgress] = []
        for i, step in enumerate(steps):
            if i < 2:
                status = StepStatus.DONE.value
            elif i == 2:
                status = StepStatus.IN_PROGRESS.value
            else:
                status = StepStatus.TODO.value
            done = status == StepStatus.DONE.value
            is_expat = step.id in expat_step_ids
            progresses.append(
                CaseStepProgress(
                    case_id=case.id,
                    template_step_id=step.id,
                    status=status,
                    responsible_type="expat" if is_expat else "agent",
                    responsible_agent_id=None if is_expat else owner.id,
                    validated_by_type="agent",
                    completed_at=(now - timedelta(days=20 - 4 * i)) if done else None,
                    completed_by_agent_id=owner.id if done else None,
                )
            )
        db.add_all(progresses)
        await db.flush()

        # --- one sample document + one client message (the thread feels real) ------
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
        db.add(
            StepComment(
                case_step_progress_id=progresses[2].id,
                author_type=ActorType.EXPAT.value,
                author_id=expat.id,
                body=_DEMO_COMMENT,
                created_at=now - timedelta(days=2),
            )
        )
        if first_case is None:
            first_case = case

    agency.settings = _demo_settings(agency, now)
    await db.commit()
    logger.info("demo cases seeded for agency %s (%d dossiers)", agency.slug, len(cloned))
    return first_case

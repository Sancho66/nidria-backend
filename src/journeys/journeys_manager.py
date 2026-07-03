import asyncio
import logging
import uuid
from collections import defaultdict

from fastapi import UploadFile
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agency import Agency
from shared.models.agent import Agent
from shared.models.journey import (
    JourneySection,
    JourneyStepParticipant,
    JourneyTemplate,
    JourneyTemplateCaseField,
    JourneyTemplateField,
    JourneyTemplateStep,
    StepPrerequisite,
)
from shared.models.step_case_requirement import StepCaseRequirement
from shared.models.step_requirement import StepRequirement
from src.cases.case_fields import COLLECTABLE_CASE_FIELDS
from src.core import storage
from src.core.config import get_settings
from src.core.enums import (
    ActorType,
    CompletionMode,
    ResponsibleType,
    StepRequirementKind,
    StepValidatorType,
)
from src.core.exceptions import (
    ConflictError,
    NotFoundError,
    PayloadTooLargeError,
    ValidationError,
)
from src.core.i18n import (
    DEFAULT_LANG,
    SUPPORTED_LANGUAGES,
    apply_i18n_write,
    normalize_i18n_input,
    resolve_i18n,
)
from src.custom_fields.custom_fields_repository import CustomFieldsRepository
from src.journeys.journeys_repository import JourneysRepository
from src.journeys.journeys_schema import (
    CanvasLayoutRequest,
    CanvasNodePosition,
    CaseFieldCreateRequest,
    CaseFieldUpdateRequest,
    JourneySectionDetail,
    JourneySectionResponse,
    JourneyTemplateDetailResponse,
    JourneyTemplateUpdateRequest,
    SectionCreateRequest,
    SectionUpdateRequest,
    StepAttachmentResponse,
    StepCaseRequirementCreateRequest,
    StepParticipantCreateRequest,
    StepRequirementCreateRequest,
    TemplateCaseFieldResponse,
    TemplateFieldCreateRequest,
    TemplateFieldResponse,
    TemplateFieldUpdateRequest,
    TemplateStepCreateRequest,
    TemplateStepParticipantResponse,
    TemplateStepResponse,
    TemplateStepUpdateRequest,
    UnsectionedFields,
)
from src.progress.requirements_eval import COLLECTABLE_BASE_FIELDS
from src.usage.usage_manager import UsageManager

logger = logging.getLogger(__name__)


def _has_cycle(graph: dict[uuid.UUID, set[uuid.UUID]]) -> bool:
    """Iterative DFS, three colors. `graph[s]` = prerequisites of s."""
    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[uuid.UUID, int] = defaultdict(int)
    for start in graph:
        if color[start] != WHITE:
            continue
        stack: list[tuple[uuid.UUID, bool]] = [(start, False)]
        while stack:
            node, processed = stack.pop()
            if processed:
                color[node] = BLACK
                continue
            if color[node] == GRAY:
                continue
            color[node] = GRAY
            stack.append((node, True))
            for dep in graph.get(node, set()):
                if color[dep] == GRAY:
                    return True
                if color[dep] == WHITE:
                    stack.append((dep, False))
    return False


def _reconcile_validator(
    validated_by_type: StepValidatorType | None,
    completion_mode: CompletionMode | None,
) -> tuple[str, str]:
    """Keep "Action validée par" (new) and completion_mode (legacy, kept as
    a rollback fallback) coherent on every write. validated_by_type wins
    when present (only IT can express expat/external); else derive it from
    completion_mode; else default to agency validation. Mapping: none⇄auto,
    {expat,agent,external}⇄agency_validation. Returns (validated_by_type,
    completion_mode) as stored strings."""
    if validated_by_type is not None:
        vt = validated_by_type.value
        cm = (
            CompletionMode.AUTO.value
            if vt == StepValidatorType.NONE.value
            else CompletionMode.AGENCY_VALIDATION.value
        )
        return vt, cm
    if completion_mode is not None:
        cm = completion_mode.value
        vt = (
            StepValidatorType.NONE.value
            if cm == CompletionMode.AUTO.value
            else StepValidatorType.AGENT.value
        )
        return vt, cm
    return StepValidatorType.AGENT.value, CompletionMode.AGENCY_VALIDATION.value


class JourneysManager:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.repo = JourneysRepository(db)

    # --- templates --------------------------------------------------------------

    async def list_templates(self, agent: Agent) -> list[JourneyTemplate]:
        return await self.repo.list_templates(agent.agency_id)

    async def list_sample_templates(self) -> list[JourneyTemplate]:
        """The shared library samples — global, read-only (any agent reads
        them; agency scoping does not apply, samples are agency-less)."""
        return await self.repo.list_sample_templates()

    async def get_clone_source(self, agent: Agent, template_id: uuid.UUID) -> JourneyTemplate:
        """Resolve a clone SOURCE: the agency's own template OR a library
        sample. Read-only (the deep clone is a later block). 404 if neither."""
        template = await self.repo.get_template_for_clone(agent.agency_id, template_id)
        if template is None:
            raise NotFoundError("Journey template not found.", code="journey.template_not_found")
        return template

    async def _get_template(self, agent: Agent, template_id: uuid.UUID) -> JourneyTemplate:
        template = await self.repo.get_template_in_agency(agent.agency_id, template_id)
        if template is None:
            raise NotFoundError("Journey template not found.", code="journey.template_not_found")
        return template

    async def agency_default(self, agency_id: uuid.UUID) -> str:
        """The agency's default content language (i18n fallback) — DEFAULT_LANG
        if the agency vanished."""
        stmt = select(Agency.default_language).where(Agency.id == agency_id)
        return (await self.db.execute(stmt)).scalar_one_or_none() or DEFAULT_LANG

    async def get_template_detail(
        self, agent: Agent, template_id: uuid.UUID, lang: str = DEFAULT_LANG
    ) -> JourneyTemplateDetailResponse:
        template = await self._get_template(agent, template_id)
        # Usage counters for the delete UX: active cases block deletion;
        # archived ones are auto-detached (the UI warns before deleting).
        active_cases_count = await self.repo.count_active_cases_using_template(template_id)
        archived_cases_count = await self.repo.count_archived_cases_using_template(template_id)
        # i18n: own template → resolve step/section labels for `lang`, falling
        # back to the agency default. (template.name has no i18n blob — BLOC 1
        # excluded it — so it stays the scalar.)
        agency_default = await self.agency_default(agent.agency_id)
        steps = await self.repo.list_steps(template_id)
        prerequisites = await self.repo.list_prerequisites(template_id)
        by_step: dict[uuid.UUID, list[uuid.UUID]] = defaultdict(list)
        for row in prerequisites:
            by_step[row.step_id].append(row.prerequisite_step_id)
        fields = await self.repo.list_fields(template_id)
        case_fields = await self.repo.list_case_fields(template_id)
        sections = await self.repo.list_sections(template_id)
        # FLAT lists (every field, all sections) — kept so the existing
        # front works unchanged.
        field_resps = await self._field_responses(agent, fields)
        case_resps = [TemplateCaseFieldResponse.model_validate(c) for c in case_fields]
        # GROUPED view (sections chantier). Both lists already come in
        # position order from the repo, so each bucket stays ordered;
        # OPTION 1 segmentation is structural (two lists per section).
        fields_by_section: dict[uuid.UUID | None, list[TemplateFieldResponse]] = defaultdict(list)
        for fr in field_resps:
            fields_by_section[fr.section_id].append(fr)
        case_by_section: dict[uuid.UUID | None, list[TemplateCaseFieldResponse]] = defaultdict(list)
        for cr in case_resps:
            case_by_section[cr.section_id].append(cr)
        # Feature 2 — step attachments, batched (no N+1), grouped per step.
        attachments = await self.repo.list_step_attachments_for_steps([s.id for s in steps])
        attach_by_step: dict[uuid.UUID, list[StepAttachmentResponse]] = defaultdict(list)
        for a in attachments:
            attach_by_step[a.step_id].append(StepAttachmentResponse.model_validate(a))
        # "Action à réaliser par" — template participants, batched (no N+1).
        participants = await self.repo.list_step_participants_for_steps([s.id for s in steps])
        participants_by_step: dict[uuid.UUID, list[TemplateStepParticipantResponse]] = defaultdict(
            list
        )
        for p in participants:
            participants_by_step[p.step_id].append(
                TemplateStepParticipantResponse.model_validate(p)
            )
        return JourneyTemplateDetailResponse(
            id=template.id,
            name=resolve_i18n(template.name_i18n, lang, agency_default, template.name),
            name_i18n=template.name_i18n,
            steps=[
                TemplateStepResponse(
                    id=step.id,
                    name=resolve_i18n(step.name_i18n, lang, agency_default, step.name),
                    name_i18n=step.name_i18n,
                    position=step.position,
                    estimated_days=step.estimated_days,
                    default_responsible_type=step.default_responsible_type,
                    default_responsible_agent_id=step.default_responsible_agent_id,
                    completion_mode=step.completion_mode,
                    default_validated_by_type=step.default_validated_by_type,
                    default_validated_by_agent_id=step.default_validated_by_agent_id,
                    prerequisite_step_ids=by_step.get(step.id, []),
                    content_note=resolve_i18n(
                        step.content_note_i18n, lang, agency_default, step.content_note
                    ),
                    content_note_i18n=step.content_note_i18n,
                    attachments=attach_by_step.get(step.id, []),
                    participants=participants_by_step.get(step.id, []),
                )
                for step in steps
            ],
            fields=field_resps,
            case_fields=case_resps,
            sections=[
                JourneySectionDetail(
                    id=s.id,
                    name=resolve_i18n(s.name_i18n, lang, agency_default, s.name),
                    name_i18n=s.name_i18n,
                    description=resolve_i18n(
                        s.description_i18n, lang, agency_default, s.description
                    ),
                    description_i18n=s.description_i18n,
                    position=s.position,
                    fields=fields_by_section.get(s.id, []),
                    case_fields=case_by_section.get(s.id, []),
                )
                for s in sections
            ],
            unsectioned=UnsectionedFields(
                fields=fields_by_section.get(None, []),
                case_fields=case_by_section.get(None, []),
            ),
            canvas_layout=template.canvas_layout,
            active_cases_count=active_cases_count,
            archived_cases_count=archived_cases_count,
            editing_language=template.editing_language,
        )

    async def set_canvas_layout(
        self, agent: Agent, template_id: uuid.UUID, payload: CanvasLayoutRequest
    ) -> dict[str, CanvasNodePosition]:
        """Replace the canvas layout blob (MVP-1). Pure presentation — no
        journey logic touched. Foreign/stale step ids are DROPPED so the
        blob never rots (only ids of the template's current steps survive)."""
        template = await self._get_template(agent, template_id)
        step_ids = {s.id for s in await self.repo.list_steps(template_id)}
        blob = {
            str(sid): {"x": pos.x, "y": pos.y}
            for sid, pos in payload.positions.items()
            if sid in step_ids
        }
        template.canvas_layout = blob
        await self.db.commit()
        return {k: CanvasNodePosition(**v) for k, v in blob.items()}

    async def create_template(
        self, agent: Agent, name: str, name_i18n: dict[str, str] | None = None
    ) -> JourneyTemplate:
        agency_default = await self.agency_default(agent.agency_id)
        scalar, blob = apply_i18n_write(name_i18n, name, agency_default, None, {})
        template = self.repo.add_template(agent.agency_id, scalar or name)
        template.name_i18n = blob
        await UsageManager(self.db).emit(
            agency_id=agent.agency_id,
            event_type="journey.created",
            actor_type=ActorType.AGENT,
            actor_id=agent.id,
        )
        await self.db.commit()
        await self.db.refresh(template)
        return template

    async def clone_template(
        self, agent: Agent, template_id: uuid.UUID, name: str | None
    ) -> JourneyTemplate:
        """Deep-clone a SOURCE (a library sample OR the agency's own template,
        resolved via get_clone_source — a foreign agency's template is 404)
        into the CALLING agency, in ONE transaction (all-or-nothing). Every id
        is remapped (sections, steps) so the clone shares NOTHING with the
        source: no shared step, no prerequisite pointing at a source step, no
        canvas key referencing a source step. The clone is never a sample
        (is_sample=False). Attachments (journey_step_attachment) are NOT
        cloned — deliberate (the file lives on the source template).
        Relaunching clones again (a copy on demand, not idempotent-dedup)."""
        source = await self.get_clone_source(agent, template_id)
        src_sections = await self.repo.list_sections(template_id)
        src_steps = await self.repo.list_steps(template_id)
        src_prereqs = await self.repo.list_prerequisites(template_id)
        src_fields = await self.repo.list_fields(template_id)
        src_case_fields = await self.repo.list_case_fields(template_id)
        src_participants = await self.repo.list_step_participants_for_steps(
            [s.id for s in src_steps]
        )

        # Parents with EXPLICIT ids → children can FK them, and the old→new
        # maps are built before any flush.
        new_template = JourneyTemplate(
            id=uuid.uuid4(),
            agency_id=agent.agency_id,
            is_sample=False,  # a clone is NEVER a sample
            name=name or f"{source.name} (copie)",
            # Copy the i18n name blob; on an EXPLICIT rename, the chosen name is
            # the new scalar and the blob is dropped (it described the source).
            name_i18n=dict(source.name_i18n) if name is None else {},
            country=source.country,  # keep the model's country of origin
        )
        self.db.add(new_template)
        # Insert the template FIRST so sections/steps FK it (no ORM
        # relationship to topologically order the inserts otherwise).
        await self.db.flush()

        section_map: dict[uuid.UUID, uuid.UUID] = {}
        for sec in src_sections:
            nid = uuid.uuid4()
            section_map[sec.id] = nid
            self.db.add(
                JourneySection(
                    id=nid,
                    template_id=new_template.id,
                    name=sec.name,
                    description=sec.description,
                    name_i18n=dict(sec.name_i18n),  # copy i18n blobs (independent)
                    description_i18n=dict(sec.description_i18n),
                    position=sec.position,
                )
            )

        step_map: dict[uuid.UUID, uuid.UUID] = {}
        for st in src_steps:
            nid = uuid.uuid4()
            step_map[st.id] = nid
            self.db.add(
                JourneyTemplateStep(
                    id=nid,
                    template_id=new_template.id,
                    name=st.name,
                    position=st.position,
                    estimated_days=st.estimated_days,
                    default_responsible_type=st.default_responsible_type,
                    default_responsible_agent_id=st.default_responsible_agent_id,
                    completion_mode=st.completion_mode,
                    default_validated_by_type=st.default_validated_by_type,
                    default_validated_by_agent_id=st.default_validated_by_agent_id,
                    content_note=st.content_note,
                    name_i18n=dict(st.name_i18n),  # copy i18n blobs (independent)
                    content_note_i18n=dict(st.content_note_i18n),
                )
            )
        # Template + sections + steps exist before their children FK them.
        await self.db.flush()

        for pr in src_prereqs:
            self.db.add(
                StepPrerequisite(
                    step_id=step_map[pr.step_id],
                    prerequisite_step_id=step_map[pr.prerequisite_step_id],
                )
            )
        for st in src_steps:
            for r in await self.repo.list_requirements(st.id):
                self.db.add(
                    StepRequirement(
                        step_id=step_map[st.id],
                        kind=r.kind,
                        reference=r.reference,
                        scope=r.scope,
                        position=r.position,
                    )
                )
            for cr in await self.repo.list_step_case_requirements(st.id):
                self.db.add(
                    StepCaseRequirement(
                        step_id=step_map[st.id],
                        case_field=cr.case_field,
                        position=cr.position,
                    )
                )
        for p in src_participants:
            self.db.add(
                JourneyStepParticipant(
                    step_id=step_map[p.step_id],
                    type=p.type,
                    agent_id=p.agent_id,
                    role=p.role,
                )
            )
        for f in src_fields:
            self.db.add(
                JourneyTemplateField(
                    template_id=new_template.id,
                    kind=f.kind,
                    reference=f.reference,
                    position=f.position,
                    required_at_creation=f.required_at_creation,
                    section_id=section_map.get(f.section_id) if f.section_id else None,
                )
            )
        for cf in src_case_fields:
            self.db.add(
                JourneyTemplateCaseField(
                    template_id=new_template.id,
                    case_field=cf.case_field,
                    position=cf.position,
                    required_at_creation=cf.required_at_creation,
                    section_id=section_map.get(cf.section_id) if cf.section_id else None,
                )
            )
        # Canvas layout: remap step-id KEYS; drop any stale/non-mapped key so
        # no source step id survives in the clone.
        if source.canvas_layout:
            remapped: dict[str, object] = {}
            for key, pos in source.canvas_layout.items():
                try:
                    old = uuid.UUID(key)
                except ValueError:
                    continue
                new = step_map.get(old)
                if new is not None:
                    remapped[str(new)] = pos
            new_template.canvas_layout = remapped or None

        await UsageManager(self.db).emit(
            agency_id=agent.agency_id,
            event_type="journey.created",
            actor_type=ActorType.AGENT,
            actor_id=agent.id,
            details={"cloned_from": str(template_id)},
        )
        await self.db.commit()
        await self.db.refresh(new_template)
        return new_template

    async def update_template(
        self, agent: Agent, template_id: uuid.UUID, payload: JourneyTemplateUpdateRequest
    ) -> JourneyTemplate:
        template = await self._get_template(agent, template_id)
        if payload.name is not None or payload.name_i18n is not None:
            agency_default = await self.agency_default(agent.agency_id)
            scalar, blob = apply_i18n_write(
                payload.name_i18n, payload.name, agency_default, template.name, template.name_i18n
            )
            template.name = scalar or template.name
            template.name_i18n = blob
        # Point 6c — editor preference only, read by no resolution path.
        # exclude_unset: absent = untouched, explicit null = reset.
        if "editing_language" in payload.model_fields_set:
            language = payload.editing_language
            if language is not None and language not in SUPPORTED_LANGUAGES:
                raise ValidationError(
                    f"Unsupported editing language {language!r}. "
                    f"Allowed: {sorted(SUPPORTED_LANGUAGES)}.",
                    code="journey.language_unsupported",
                    params={"language": language, "allowed": sorted(SUPPORTED_LANGUAGES)},
                )
            template.editing_language = language
        await self.db.commit()
        await self.db.refresh(template)
        return template

    async def delete_template(self, agent: Agent, template_id: uuid.UUID) -> None:
        # _get_template enforces agency scope: only this agency's template,
        # and the detach below is scoped to template_id → never another
        # agency's / another template's data.
        template = await self._get_template(agent, template_id)
        # Only ACTIVE cases block: a journey in live use is never deleted.
        active = await self.repo.count_active_cases_using_template(template_id)
        if active:
            raise ConflictError(
                f"Template is assigned to {active} active case(s) and cannot be deleted.",
                code="journey.template_in_use",
                params={"count": active},
            )
        # ARCHIVED (soft-deleted) cases must NOT block (they're invisible in
        # the UI): detach them — null the link + purge their step instances of
        # THIS template — then delete the template, ALL in one transaction.
        await self.repo.detach_archived_cases_from_template(template_id)
        await self.repo.delete_template(template)
        try:
            await self.db.commit()
        except IntegrityError as exc:
            # Safety net for any FK violation we did not anticipate (e.g.
            # orphan case_step_progress of a since-reassigned case): a clean
            # 409, never a bare RESTRICT 500. Nothing is half-detached — the
            # whole transaction rolls back.
            await self.db.rollback()
            raise ConflictError(
                "This journey is still in use and cannot be deleted.",
                code="journey.template_still_referenced",
            ) from exc

    # --- steps -------------------------------------------------------------------

    async def _validate_default_responsible_agent(
        self, agent: Agent, agent_id: uuid.UUID | None
    ) -> None:
        """A template's named default responsible must belong to the
        template's agency — INTERNAL agent OR durable EXTERNAL partner
        (revised model). Another agency's agent is always rejected; the
        external case-assignment is auto-created at instantiation."""
        if agent_id is None:
            return
        target = await self.repo.get_agent_in_agency(agent.agency_id, agent_id)
        if target is None:
            raise ValidationError(
                "Default responsible must belong to this agency.",
                code="journey.responsible_not_in_agency",
            )

    async def add_step(
        self, agent: Agent, template_id: uuid.UUID, payload: TemplateStepCreateRequest
    ) -> JourneyTemplateStep:
        await self._get_template(agent, template_id)
        await self._validate_default_responsible_agent(agent, payload.default_responsible_agent_id)
        # The validator default agent is validated like the responsible one
        # (internal member or durable external partner of the agency).
        await self._validate_default_responsible_agent(agent, payload.default_validated_by_agent_id)
        validated_by_type, completion_mode = _reconcile_validator(
            payload.validated_by_type, payload.completion_mode
        )
        agency_default = await self.agency_default(agent.agency_id)
        name_scalar, name_blob = apply_i18n_write(
            payload.name_i18n, payload.name, agency_default, None, {}
        )
        max_position = await self.repo.max_position(template_id)
        step = self.repo.add_step(
            template_id=template_id,
            name=name_scalar or payload.name,
            position=(max_position if max_position is not None else -1) + 1,
            estimated_days=payload.estimated_days,
            default_responsible_type=payload.default_responsible_type,
            default_responsible_agent_id=payload.default_responsible_agent_id,
            completion_mode=completion_mode,
            default_validated_by_type=validated_by_type,
            default_validated_by_agent_id=payload.default_validated_by_agent_id,
        )
        step.name_i18n = name_blob
        await self.db.flush()
        # Option-A backfill: on an ASSIGNED template, the new step is
        # instantiated on every live case (same transaction as the
        # step creation — atomic).
        from src.progress.progress_manager import ProgressManager

        await UsageManager(self.db).emit(
            agency_id=agent.agency_id,
            event_type="journey.step_added",
            actor_type=ActorType.AGENT,
            actor_id=agent.id,
            details={"template_id": str(template_id)},
        )
        await ProgressManager(self.db).backfill_step(agent, step)
        await self.db.commit()
        await self.db.refresh(step)
        return step

    async def _get_step(self, template_id: uuid.UUID, step_id: uuid.UUID) -> JourneyTemplateStep:
        step = await self.repo.get_step_in_template(template_id, step_id)
        if step is None:
            raise NotFoundError("Template step not found.", code="journey.step_not_found")
        return step

    async def update_step(
        self,
        agent: Agent,
        template_id: uuid.UUID,
        step_id: uuid.UUID,
        payload: TemplateStepUpdateRequest,
    ) -> JourneyTemplateStep:
        await self._get_template(agent, template_id)
        step = await self._get_step(template_id, step_id)
        changes = payload.model_dump(exclude_unset=True)
        if "default_responsible_agent_id" in changes:
            await self._validate_default_responsible_agent(
                agent, changes["default_responsible_agent_id"]
            )
        if "default_validated_by_agent_id" in changes:
            await self._validate_default_responsible_agent(
                agent, changes["default_validated_by_agent_id"]
            )
        # Validator coherence: the payload exposes `validated_by_type` /
        # `completion_mode`, but they map to ONE template column pair kept in
        # sync. Resolve them out of the generic setattr loop (and note the
        # payload field name `validated_by_type` ≠ the column
        # `default_validated_by_type`).
        if "validated_by_type" in changes or "completion_mode" in changes:
            vt, cm = _reconcile_validator(payload.validated_by_type, payload.completion_mode)
            step.default_validated_by_type = vt
            step.completion_mode = cm
        for key in ("validated_by_type", "completion_mode"):
            changes.pop(key, None)
        # i18n write (BLOC 2bis): name & content_note resolve scalar+blob via
        # apply_i18n_write, OUT of the generic loop (so the scalar stays in sync
        # with the blob). content_note=None explicitly clears both.
        if "name" in changes or "name_i18n" in changes:
            agency_default = await self.agency_default(agent.agency_id)
            scalar, blob = apply_i18n_write(
                payload.name_i18n if "name_i18n" in changes else None,
                payload.name if "name" in changes else None,
                agency_default,
                step.name,
                step.name_i18n,
            )
            step.name = scalar or step.name
            step.name_i18n = blob
        if "content_note" in changes or "content_note_i18n" in changes:
            agency_default = await self.agency_default(agent.agency_id)
            if "content_note" in changes and payload.content_note is None:
                step.content_note = None  # explicit clear
                step.content_note_i18n = normalize_i18n_input(payload.content_note_i18n)
            else:
                scalar, blob = apply_i18n_write(
                    payload.content_note_i18n if "content_note_i18n" in changes else None,
                    payload.content_note if "content_note" in changes else None,
                    agency_default,
                    step.content_note,
                    step.content_note_i18n,
                )
                step.content_note = scalar
                step.content_note_i18n = blob
        for key in ("name", "name_i18n", "content_note", "content_note_i18n"):
            changes.pop(key, None)
        for field, value in changes.items():
            setattr(step, field, value)
        await self.db.commit()
        await self.db.refresh(step)
        return step

    async def delete_step(self, agent: Agent, template_id: uuid.UUID, step_id: uuid.UUID) -> None:
        await self._get_template(agent, template_id)
        step = await self._get_step(template_id, step_id)
        assigned = await self.repo.count_cases_using_template(template_id)
        if assigned:
            # Removing a step from a LIVE process is qualitatively
            # different from adjusting it (instances would dangle);
            # RESTRICT would block it anyway once instantiated — make
            # it a clean, systematic 409 instead.
            raise ConflictError(
                f"Template is assigned to {assigned} case(s); its steps cannot be deleted.",
                code="journey.step_in_use",
                params={"count": assigned},
            )
        await self.repo.delete_step(step)
        await self.db.flush()
        await self._renumber_dense(template_id)
        await self.db.commit()

    # --- step participants ("Action à réaliser par", N — agency CRUD) --------------

    async def list_step_participants(
        self, agent: Agent, template_id: uuid.UUID, step_id: uuid.UUID
    ) -> list[TemplateStepParticipantResponse]:
        await self._get_template(agent, template_id)
        await self._get_step(template_id, step_id)
        rows = await self.repo.list_step_participants_for_steps([step_id])
        return [TemplateStepParticipantResponse.model_validate(r) for r in rows]

    async def add_step_participant(
        self,
        agent: Agent,
        template_id: uuid.UUID,
        step_id: uuid.UUID,
        payload: StepParticipantCreateRequest,
    ) -> TemplateStepParticipantResponse:
        """Add a participant on a template step. type ∈ {expat, agent} —
        an external_contact is case-scoped, not addressable at the template
        (same limit as the responsible). `agent` (internal OR durable
        external) requires an agent of this agency; `expat` carries no agent.
        Does NOT touch the responsible, the validator, or live dossiers
        (snapshot: only new cases inherit; edit per-case stays per-case)."""
        await self._get_template(agent, template_id)
        await self._get_step(template_id, step_id)
        if payload.type is ResponsibleType.EXTERNAL:
            raise ValidationError(
                "A template participant is 'expat' or 'agent' "
                "(name a durable external as 'agent').",
                code="journey.participant_type_invalid",
            )
        if payload.type is ResponsibleType.EXPAT:
            if payload.agent_id is not None:
                raise ValidationError(
                    "An 'expat' participant carries no agent.",
                    code="journey.participant_expat_no_agent",
                )
            agent_id = None
        else:  # AGENT — a named member, OR agent_id NULL = "the agency in general"
            if payload.agent_id is not None:
                await self._validate_default_responsible_agent(agent, payload.agent_id)
            agent_id = payload.agent_id
        row = self.repo.add_step_participant(
            step_id=step_id, type=payload.type.value, agent_id=agent_id, role=payload.role.value
        )
        await self.db.commit()
        await self.db.refresh(row)
        return TemplateStepParticipantResponse.model_validate(row)

    async def delete_step_participant(
        self, agent: Agent, template_id: uuid.UUID, step_id: uuid.UUID, participant_id: uuid.UUID
    ) -> None:
        await self._get_template(agent, template_id)
        await self._get_step(template_id, step_id)
        row = await self.repo.get_step_participant_in_step(step_id, participant_id)
        if row is None:
            raise NotFoundError("Participant not found.", code="journey.participant_not_found")
        await self.repo.delete_step_participant(row)
        await self.db.commit()

    # --- step content: attachments (Feature 2, V1 — agency CRUD) -------------------
    # content_note is handled by update_step (a column on the step). The
    # files reuse the generic storage primitive (NOT the case-scoped
    # `document` table): they live on the template, shared by every case.

    async def list_step_attachments(
        self, agent: Agent, template_id: uuid.UUID, step_id: uuid.UUID
    ) -> list[StepAttachmentResponse]:
        await self._get_template(agent, template_id)
        await self._get_step(template_id, step_id)
        rows = await self.repo.list_step_attachments(step_id)
        return [StepAttachmentResponse.model_validate(r) for r in rows]

    async def add_step_attachment(
        self, agent: Agent, template_id: uuid.UUID, step_id: uuid.UUID, file: UploadFile
    ) -> StepAttachmentResponse:
        await self._get_template(agent, template_id)
        await self._get_step(template_id, step_id)
        settings = get_settings()
        original = file.filename
        if not original:
            raise ValidationError(
                "A filename is required.", code="journey.attachment_filename_required"
            )
        ext = original.rsplit(".", 1)[-1].lower() if "." in original else ""
        if ext not in settings.allowed_document_extensions:
            allowed = ", ".join(settings.allowed_document_extensions)
            raise ValidationError(
                f"File type not allowed (accepted: {allowed}).",
                code="journey.attachment_type_not_allowed",
                params={"accepted": sorted(settings.allowed_document_extensions)},
            )
        content = await file.read()
        if len(content) > settings.max_document_size_mb * 1024 * 1024:
            raise PayloadTooLargeError(
                f"File exceeds the {settings.max_document_size_mb} MB limit.",
                code="journey.attachment_too_large",
                params={"max_mb": settings.max_document_size_mb},
            )

        attachment_id = uuid.uuid4()
        path = (
            f"templates/{template_id}/steps/{step_id}/{attachment_id}/"
            f"{storage.sanitize_filename(original)}"
        )
        max_pos = await self.repo.max_attachment_position(step_id)
        # Storage FIRST, then the DB row. If the insert fails, delete the
        # uploaded file so there is no orphan in storage (coherence).
        await asyncio.to_thread(
            storage.upload, path, content, file.content_type or "application/octet-stream"
        )
        try:
            row = self.repo.add_step_attachment(
                id=attachment_id,
                step_id=step_id,
                filename=original,
                storage_path=path,
                uploaded_by_agent_id=agent.id,
                position=(max_pos if max_pos is not None else -1) + 1,
            )
            await self.db.commit()
            await self.db.refresh(row)
        except Exception:
            await asyncio.to_thread(storage.delete, path)  # no orphan file
            raise
        return StepAttachmentResponse.model_validate(row)

    async def download_step_attachment(
        self, agent: Agent, template_id: uuid.UUID, step_id: uuid.UUID, attachment_id: uuid.UUID
    ) -> tuple[str, bytes]:
        await self._get_template(agent, template_id)
        await self._get_step(template_id, step_id)
        row = await self.repo.get_step_attachment_in_step(step_id, attachment_id)
        if row is None:
            raise NotFoundError("Attachment not found.", code="journey.attachment_not_found")
        content = await asyncio.to_thread(storage.download, row.storage_path)
        return row.filename, content

    async def delete_step_attachment(
        self, agent: Agent, template_id: uuid.UUID, step_id: uuid.UUID, attachment_id: uuid.UUID
    ) -> None:
        await self._get_template(agent, template_id)
        await self._get_step(template_id, step_id)
        row = await self.repo.get_step_attachment_in_step(step_id, attachment_id)
        if row is None:
            raise NotFoundError("Attachment not found.", code="journey.attachment_not_found")
        path = row.storage_path
        await self.repo.delete_step_attachment(row)
        await self.db.commit()
        # Remove the file too (no orphan). Best-effort AFTER the row is gone:
        # a storage hiccup leaves an orphan file (logged), never a row that
        # points to a missing file.
        try:
            await asyncio.to_thread(storage.delete, path)
        except Exception:
            logger.warning("step attachment file not deleted (orphan) path=%s", path)

    async def _renumber_dense(self, template_id: uuid.UUID) -> None:
        """Two-phase renumbering: shift every position out of range with
        one UPDATE (no transient unique violation), then assign 0..n-1
        in current order."""
        steps = await self.repo.list_steps(template_id)
        if not steps:
            return
        offset = max(step.position for step in steps) + len(steps) + 1
        await self.repo.shift_positions(template_id, offset)
        for index, step in enumerate(steps):
            await self.repo.set_position(step.id, index)

    async def reorder_steps(
        self, agent: Agent, template_id: uuid.UUID, step_ids: list[uuid.UUID]
    ) -> list[JourneyTemplateStep]:
        await self._get_template(agent, template_id)
        steps = await self.repo.list_steps(template_id)
        if len(step_ids) != len(set(step_ids)) or set(step_ids) != {s.id for s in steps}:
            raise ValidationError(
                "step_ids must contain exactly the template's steps, once each.",
                code="journey.step_order_mismatch",
            )
        offset = max(step.position for step in steps) + len(steps) + 1
        await self.repo.shift_positions(template_id, offset)
        for index, step_id in enumerate(step_ids):
            await self.repo.set_position(step_id, index)
        await self.db.commit()
        return await self.repo.list_steps(template_id)

    # --- prerequisites ------------------------------------------------------------------

    async def set_prerequisites(
        self,
        agent: Agent,
        template_id: uuid.UUID,
        step_id: uuid.UUID,
        prerequisite_step_ids: list[uuid.UUID],
    ) -> None:
        """Declarative replace + full-graph validation on EVERY mutation.

        Editing prerequisites of an ASSIGNED template is allowed (option
        A): locking is resolved dynamically against the current template
        state at transition time; DONE steps are never un-validated.
        """
        await self._get_template(agent, template_id)
        await self._get_step(template_id, step_id)
        proposed = set(prerequisite_step_ids)

        if step_id in proposed:
            raise ValidationError(
                "A step cannot be its own prerequisite.", code="journey.prerequisite_self"
            )
        template_step_ids = {step.id for step in await self.repo.list_steps(template_id)}
        if not proposed <= template_step_ids:
            raise ValidationError(
                "Prerequisites must belong to the same template.",
                code="journey.prerequisite_foreign",
            )

        # Full graph = existing edges of the other steps + the proposed
        # set for this one.
        graph: dict[uuid.UUID, set[uuid.UUID]] = {sid: set() for sid in template_step_ids}
        for row in await self.repo.list_prerequisites(template_id):
            if row.step_id != step_id:
                graph[row.step_id].add(row.prerequisite_step_id)
        graph[step_id] = proposed
        if _has_cycle(graph):
            raise ValidationError(
                "This prerequisite change would create a cycle.", code="journey.prerequisite_cycle"
            )

        await self.repo.delete_prerequisites_of_step(step_id)
        for prerequisite_id in proposed:
            self.repo.add_prerequisite(step_id, prerequisite_id)
        await self.db.commit()

    # --- step requirements (NEW WAVE) ----------------------------------------------

    async def list_requirements(
        self, agent: Agent, template_id: uuid.UUID, step_id: uuid.UUID
    ) -> list[StepRequirement]:
        await self._get_template(agent, template_id)
        await self._get_step(template_id, step_id)
        return await self.repo.list_requirements(step_id)

    async def add_requirement(
        self,
        agent: Agent,
        template_id: uuid.UUID,
        step_id: uuid.UUID,
        payload: StepRequirementCreateRequest,
    ) -> StepRequirement:
        await self._get_template(agent, template_id)
        await self._get_step(template_id, step_id)
        await self._validate_reference(agent, payload.kind, payload.reference)
        # Strict membership (NEW requirements only): a base_field / custom_field
        # may only reference a field DECLARED in this template's Informations
        # tab (a journey_template_field of the SAME template). document is a free
        # label → no referential, no check. Existing rows and deep-clone copies
        # are written directly (not via this path), so they are never re-checked.
        if payload.kind in (StepRequirementKind.BASE_FIELD, StepRequirementKind.CUSTOM_FIELD):
            declared = await self.repo.get_field_by_reference(
                template_id, payload.kind.value, payload.reference
            )
            if declared is None:
                raise ValidationError(
                    f"The field {payload.reference!r} must first be added to the journey's "
                    "Informations tab before it can be requested at a step.",
                    code="journey.requirement_field_not_declared",
                    params={"reference": payload.reference},
                )
        requirement = self.repo.add_requirement(
            step_id=step_id,
            kind=payload.kind.value,
            reference=payload.reference,
            scope=payload.scope.value,
            position=payload.position,
        )
        await self.db.flush()
        # Point-8 backfill (mirror of add_step's Option-A contract, one level
        # down): live cases whose instance of THIS step is currently active
        # gain the missing concrete instances — same transaction. TODO steps
        # materialize at activation; DONE steps catch up at reopen.
        from src.progress.progress_manager import ProgressManager

        progress_manager = ProgressManager(self.db)
        pending = await progress_manager.backfill_requirements(agent, requirement)
        await self.db.commit()
        await self.db.refresh(requirement)
        await progress_manager.send_pending(pending)
        return requirement

    async def _validate_reference(
        self, agent: Agent, kind: StepRequirementKind, reference: str
    ) -> None:
        """Catalog/definition validity, shared by requirements AND Informations
        fields: base_field → whitelist; custom_field → an ACTIVE definition of
        the agency must exist (a later archive is handled at read time via
        is_archived); document → free label, nothing to check. (The strict
        membership rule — a requirement may only reference an Informations
        field — lives in add_requirement, NOT here: a field added to the
        Informations tab cannot require itself to already be there.)"""
        if kind is StepRequirementKind.BASE_FIELD:
            if reference not in COLLECTABLE_BASE_FIELDS:
                raise ValidationError(
                    f"Unknown base field {reference!r}. "
                    f"Allowed: {sorted(COLLECTABLE_BASE_FIELDS)}.",
                    code="journey.base_field_unknown",
                    params={"reference": reference, "allowed": sorted(COLLECTABLE_BASE_FIELDS)},
                )
        elif kind is StepRequirementKind.CUSTOM_FIELD:
            definition = await CustomFieldsRepository(self.db).get_by_key(
                agent.agency_id, reference
            )
            if definition is None or definition.archived_at is not None:
                raise ValidationError(
                    f"No active custom field with key {reference!r} for this agency.",
                    code="journey.custom_field_unknown",
                    params={"reference": reference},
                )

    async def reorder_requirements(
        self,
        agent: Agent,
        template_id: uuid.UUID,
        step_id: uuid.UUID,
        requirement_ids: list[uuid.UUID],
    ) -> list[StepRequirement]:
        """Same convention as reorder_steps, one level down: the payload
        is the FULL set of the step's requirement ids in the desired
        order. A foreign id (other step / other agency) makes the set
        mismatch → 422, no leak. Two-phase renumber to 0..n-1."""
        await self._get_template(agent, template_id)
        await self._get_step(template_id, step_id)
        requirements = await self.repo.list_requirements(step_id)
        if len(requirement_ids) != len(set(requirement_ids)) or set(requirement_ids) != {
            r.id for r in requirements
        }:
            raise ValidationError(
                "requirement_ids must contain exactly the step's requirements, once each.",
                code="journey.requirement_order_mismatch",
            )
        offset = max((r.position for r in requirements), default=-1) + len(requirements) + 1
        await self.repo.shift_requirement_positions(step_id, offset)
        for index, requirement_id in enumerate(requirement_ids):
            await self.repo.set_requirement_position(requirement_id, index)
        await self.db.commit()
        return await self.repo.list_requirements(step_id)

    async def delete_requirement(
        self, agent: Agent, template_id: uuid.UUID, step_id: uuid.UUID, requirement_id: uuid.UUID
    ) -> None:
        await self._get_template(agent, template_id)
        await self._get_step(template_id, step_id)
        requirement = await self.repo.get_requirement_in_step(step_id, requirement_id)
        if requirement is None:
            raise NotFoundError("Step requirement not found.", code="journey.requirement_not_found")
        await self.repo.delete_requirement(requirement)
        await self.db.commit()

    # --- step CASE requirements (sections chantier, vague C) — calque ----------------
    # A step may require a client_case column (country/address). The value
    # is NEVER stored here — derived live from client_case. Declaration +
    # CRUD only; the projection/completion fold lives in ProgressManager.

    async def list_step_case_requirements(
        self, agent: Agent, template_id: uuid.UUID, step_id: uuid.UUID
    ) -> list[StepCaseRequirement]:
        await self._get_template(agent, template_id)
        await self._get_step(template_id, step_id)
        return await self.repo.list_step_case_requirements(step_id)

    async def add_step_case_requirement(
        self,
        agent: Agent,
        template_id: uuid.UUID,
        step_id: uuid.UUID,
        payload: StepCaseRequirementCreateRequest,
    ) -> StepCaseRequirement:
        await self._get_template(agent, template_id)
        await self._get_step(template_id, step_id)
        if payload.case_field not in COLLECTABLE_CASE_FIELDS:
            raise ValidationError(
                f"Unknown case field {payload.case_field!r}. "
                f"Allowed: {sorted(COLLECTABLE_CASE_FIELDS)}.",
                code="journey.case_field_unknown",
                params={
                    "case_field": payload.case_field,
                    "allowed": sorted(COLLECTABLE_CASE_FIELDS),
                },
            )
        # Strict membership: the case field must be DECLARED in this template's
        # Informations tab (a journey_template_case_field of the SAME template).
        # NEW requirements only — existing rows and clone copies bypass this.
        declared = await self.repo.get_case_field_by_ref(template_id, payload.case_field)
        if declared is None:
            raise ValidationError(
                f"The case field {payload.case_field!r} must first be added to the "
                "journey's Informations tab before it can be requested at a step.",
                code="journey.case_field_not_declared",
                params={"case_field": payload.case_field},
            )
        existing = await self.repo.get_step_case_requirement_by_ref(step_id, payload.case_field)
        if existing is not None:
            raise ConflictError(
                f"Case field {payload.case_field!r} is already required by this step.",
                code="journey.case_requirement_duplicate",
                params={"case_field": payload.case_field},
            )
        row = self.repo.add_step_case_requirement(
            step_id=step_id, case_field=payload.case_field, position=payload.position
        )
        await self.db.commit()
        await self.db.refresh(row)
        return row

    async def reorder_step_case_requirements(
        self,
        agent: Agent,
        template_id: uuid.UUID,
        step_id: uuid.UUID,
        case_requirement_ids: list[uuid.UUID],
    ) -> list[StepCaseRequirement]:
        """Full ordered set of the step's case-requirement ids (same
        convention as reorder_requirements). Foreign/incomplete → 422.
        Two-phase dense renumber to 0..n-1."""
        await self._get_template(agent, template_id)
        await self._get_step(template_id, step_id)
        rows = await self.repo.list_step_case_requirements(step_id)
        if len(case_requirement_ids) != len(set(case_requirement_ids)) or set(
            case_requirement_ids
        ) != {r.id for r in rows}:
            raise ValidationError(
                "case_requirement_ids must contain exactly the step's case requirements, "
                "once each.",
                code="journey.case_requirement_order_mismatch",
            )
        offset = max((r.position for r in rows), default=-1) + len(rows) + 1
        await self.repo.shift_step_case_requirement_positions(step_id, offset)
        for index, case_requirement_id in enumerate(case_requirement_ids):
            await self.repo.set_step_case_requirement_position(case_requirement_id, index)
        await self.db.commit()
        return await self.repo.list_step_case_requirements(step_id)

    async def delete_step_case_requirement(
        self,
        agent: Agent,
        template_id: uuid.UUID,
        step_id: uuid.UUID,
        case_requirement_id: uuid.UUID,
    ) -> None:
        await self._get_template(agent, template_id)
        await self._get_step(template_id, step_id)
        row = await self.repo.get_step_case_requirement_in_step(step_id, case_requirement_id)
        if row is None:
            raise NotFoundError(
                "Step case requirement not found.", code="journey.case_requirement_not_found"
            )
        await self.repo.delete_step_case_requirement(row)
        await self.db.commit()

    # --- per-template field collection (NEW WAVE) — calque of requirements ---------

    async def _field_responses(
        self, agent: Agent, fields: list[JourneyTemplateField]
    ) -> list[TemplateFieldResponse]:
        """Resolve each field's render metadata, BATCHED (one fetch of the
        agency's definitions, active + archived): a custom field carries
        label/field_type/options + is_archived; a base field carries none
        (the frontend knows the civil-status set). No N+1."""
        defs = {
            d.key: d
            for d in await CustomFieldsRepository(self.db).list_for_agency(
                agent.agency_id, include_archived=True
            )
        }
        out: list[TemplateFieldResponse] = []
        for f in fields:
            label = field_type = None
            options = None
            is_archived = False
            if f.kind == StepRequirementKind.CUSTOM_FIELD.value:
                d = defs.get(f.reference)
                is_archived = d is None or d.archived_at is not None
                if d is not None:
                    label, field_type, options = d.label, d.field_type, d.options
            out.append(
                TemplateFieldResponse(
                    id=f.id,
                    template_id=f.template_id,
                    kind=f.kind,
                    reference=f.reference,
                    position=f.position,
                    required_at_creation=f.required_at_creation,
                    label=label,
                    field_type=field_type,
                    options=options,
                    is_archived=is_archived,
                    section_id=f.section_id,
                )
            )
        return out

    async def list_fields(
        self, agent: Agent, template_id: uuid.UUID
    ) -> list[TemplateFieldResponse]:
        await self._get_template(agent, template_id)
        return await self._field_responses(agent, await self.repo.list_fields(template_id))

    async def add_field(
        self, agent: Agent, template_id: uuid.UUID, payload: TemplateFieldCreateRequest
    ) -> TemplateFieldResponse:
        await self._get_template(agent, template_id)
        if payload.kind is StepRequirementKind.DOCUMENT:
            raise ValidationError(
                "A creation field is a base_field or custom_field (documents are requirements).",
                code="journey.field_kind_invalid",
            )
        # Same validation as requirements: base → whitelist, custom →
        # active definition of the agency.
        await self._validate_reference(agent, payload.kind, payload.reference)
        # Dedup pre-check → clean 409 (the UNIQUE(template_id, kind,
        # reference) constraint is the floor; this gives a friendly error).
        existing = await self.repo.get_field_by_reference(
            template_id, payload.kind.value, payload.reference
        )
        if existing is not None:
            raise ConflictError(
                f"Field {payload.reference!r} is already collected by this template.",
                code="journey.field_duplicate",
                params={"reference": payload.reference},
            )
        # Born-ranged creation (point 5): same-template validation as the
        # PATCH move; None stays the legitimate unsectioned bucket.
        await self._validate_section(template_id, payload.section_id)
        field = self.repo.add_field(
            template_id=template_id,
            kind=payload.kind.value,
            reference=payload.reference,
            position=payload.position,
            required_at_creation=payload.required_at_creation,
            section_id=payload.section_id,
        )
        await self.db.commit()
        await self.db.refresh(field)
        return (await self._field_responses(agent, [field]))[0]

    async def reorder_fields(
        self, agent: Agent, template_id: uuid.UUID, field_ids: list[uuid.UUID]
    ) -> list[TemplateFieldResponse]:
        """Full ordered set of the template's field ids (same convention
        as reorder_steps / reorder_requirements). A foreign/incomplete set
        → 422. Two-phase dense renumber to 0..n-1."""
        await self._get_template(agent, template_id)
        fields = await self.repo.list_fields(template_id)
        if len(field_ids) != len(set(field_ids)) or set(field_ids) != {f.id for f in fields}:
            raise ValidationError(
                "field_ids must contain exactly the template's fields, once each.",
                code="journey.field_order_mismatch",
            )
        offset = max((f.position for f in fields), default=-1) + len(fields) + 1
        await self.repo.shift_field_positions(template_id, offset)
        for index, field_id in enumerate(field_ids):
            await self.repo.set_field_position(field_id, index)
        await self.db.commit()
        return await self._field_responses(agent, await self.repo.list_fields(template_id))

    async def _validate_section(self, template_id: uuid.UUID, section_id: uuid.UUID | None) -> None:
        """A field's section must belong to the SAME template (or None =
        the unsectioned bucket)."""
        if section_id is None:
            return
        if await self.repo.get_section_in_template(template_id, section_id) is None:
            raise ValidationError(
                "Section must belong to this template.", code="journey.section_foreign"
            )

    async def update_field(
        self,
        agent: Agent,
        template_id: uuid.UUID,
        field_id: uuid.UUID,
        payload: TemplateFieldUpdateRequest,
    ) -> TemplateFieldResponse:
        await self._get_template(agent, template_id)
        field = await self.repo.get_field_in_template(template_id, field_id)
        if field is None:
            raise NotFoundError("Template field not found.", code="journey.field_not_found")
        # Partial PATCH: required toggle and/or section move (exclude_unset
        # distinguishes "untouched" from "set to null").
        changes = payload.model_dump(exclude_unset=True)
        if changes.get("required_at_creation") is not None:
            field.required_at_creation = changes["required_at_creation"]
        if "section_id" in changes:
            await self._validate_section(template_id, changes["section_id"])
            field.section_id = changes["section_id"]
        await self.db.commit()
        await self.db.refresh(field)
        return (await self._field_responses(agent, [field]))[0]

    async def delete_field(self, agent: Agent, template_id: uuid.UUID, field_id: uuid.UUID) -> None:
        await self._get_template(agent, template_id)
        field = await self.repo.get_field_in_template(template_id, field_id)
        if field is None:
            raise NotFoundError("Template field not found.", code="journey.field_not_found")
        await self.repo.delete_field(field)
        await self.db.commit()

    # --- per-template CASE-field collection (option b) — countries -----------------
    # Subset of the person-field CRUD: no kind, no custom-definition
    # resolution, no is_archived. The value is NEVER stored here — it goes
    # to client_case via the existing create keys; this only declares
    # collection + the required gate + order.

    async def list_case_fields(
        self, agent: Agent, template_id: uuid.UUID
    ) -> list[TemplateCaseFieldResponse]:
        await self._get_template(agent, template_id)
        rows = await self.repo.list_case_fields(template_id)
        return [TemplateCaseFieldResponse.model_validate(c) for c in rows]

    async def add_case_field(
        self, agent: Agent, template_id: uuid.UUID, payload: CaseFieldCreateRequest
    ) -> TemplateCaseFieldResponse:
        await self._get_template(agent, template_id)
        if payload.case_field not in COLLECTABLE_CASE_FIELDS:
            raise ValidationError(
                f"Unknown case field {payload.case_field!r}. "
                f"Allowed: {sorted(COLLECTABLE_CASE_FIELDS)}.",
                code="journey.case_field_unknown",
                params={
                    "case_field": payload.case_field,
                    "allowed": sorted(COLLECTABLE_CASE_FIELDS),
                },
            )
        # Dedup pre-check → clean 409 (UNIQUE(template_id, case_field) is
        # the floor; this gives a friendly error).
        existing = await self.repo.get_case_field_by_ref(template_id, payload.case_field)
        if existing is not None:
            raise ConflictError(
                f"Case field {payload.case_field!r} is already collected by this template.",
                code="journey.case_field_duplicate",
                params={"case_field": payload.case_field},
            )
        # Born-ranged creation (point 5), mirroring add_field.
        await self._validate_section(template_id, payload.section_id)
        case_field = self.repo.add_case_field(
            template_id=template_id,
            case_field=payload.case_field,
            position=payload.position,
            required_at_creation=payload.required_at_creation,
            section_id=payload.section_id,
        )
        await self.db.commit()
        await self.db.refresh(case_field)
        return TemplateCaseFieldResponse.model_validate(case_field)

    async def reorder_case_fields(
        self, agent: Agent, template_id: uuid.UUID, case_field_ids: list[uuid.UUID]
    ) -> list[TemplateCaseFieldResponse]:
        """Full ordered set of the template's case-field ids (same
        convention as reorder_fields). A foreign/incomplete set → 422.
        Two-phase dense renumber to 0..n-1."""
        await self._get_template(agent, template_id)
        rows = await self.repo.list_case_fields(template_id)
        if len(case_field_ids) != len(set(case_field_ids)) or set(case_field_ids) != {
            c.id for c in rows
        }:
            raise ValidationError(
                "case_field_ids must contain exactly the template's case fields, once each.",
                code="journey.case_field_order_mismatch",
            )
        offset = max((c.position for c in rows), default=-1) + len(rows) + 1
        await self.repo.shift_case_field_positions(template_id, offset)
        for index, case_field_id in enumerate(case_field_ids):
            await self.repo.set_case_field_position(case_field_id, index)
        await self.db.commit()
        reordered = await self.repo.list_case_fields(template_id)
        return [TemplateCaseFieldResponse.model_validate(c) for c in reordered]

    async def update_case_field(
        self,
        agent: Agent,
        template_id: uuid.UUID,
        case_field_id: uuid.UUID,
        payload: CaseFieldUpdateRequest,
    ) -> TemplateCaseFieldResponse:
        await self._get_template(agent, template_id)
        case_field = await self.repo.get_case_field_in_template(template_id, case_field_id)
        if case_field is None:
            raise NotFoundError(
                "Template case field not found.", code="journey.case_field_not_found"
            )
        changes = payload.model_dump(exclude_unset=True)
        if changes.get("required_at_creation") is not None:
            case_field.required_at_creation = changes["required_at_creation"]
        if "section_id" in changes:
            await self._validate_section(template_id, changes["section_id"])
            case_field.section_id = changes["section_id"]
        await self.db.commit()
        await self.db.refresh(case_field)
        return TemplateCaseFieldResponse.model_validate(case_field)

    async def delete_case_field(
        self, agent: Agent, template_id: uuid.UUID, case_field_id: uuid.UUID
    ) -> None:
        await self._get_template(agent, template_id)
        case_field = await self.repo.get_case_field_in_template(template_id, case_field_id)
        if case_field is None:
            raise NotFoundError(
                "Template case field not found.", code="journey.case_field_not_found"
            )
        await self.repo.delete_case_field(case_field)
        await self.db.commit()

    # --- sections (sections chantier, vague A) — additive socle --------------------

    async def list_sections(
        self, agent: Agent, template_id: uuid.UUID
    ) -> list[JourneySectionResponse]:
        await self._get_template(agent, template_id)
        rows = await self.repo.list_sections(template_id)
        return [JourneySectionResponse.model_validate(s) for s in rows]

    async def add_section(
        self, agent: Agent, template_id: uuid.UUID, payload: SectionCreateRequest
    ) -> JourneySectionResponse:
        await self._get_template(agent, template_id)
        agency_default = await self.agency_default(agent.agency_id)
        name_scalar, name_blob = apply_i18n_write(
            payload.name_i18n, payload.name, agency_default, None, {}
        )
        desc_scalar, desc_blob = apply_i18n_write(
            payload.description_i18n, payload.description, agency_default, None, {}
        )
        max_position = await self.repo.max_section_position(template_id)
        section = self.repo.add_section(
            template_id=template_id,
            name=name_scalar or payload.name,
            description=desc_scalar,
            position=(max_position if max_position is not None else -1) + 1,
        )
        section.name_i18n = name_blob
        section.description_i18n = desc_blob
        await self.db.commit()
        await self.db.refresh(section)
        return JourneySectionResponse.model_validate(section)

    async def update_section(
        self,
        agent: Agent,
        template_id: uuid.UUID,
        section_id: uuid.UUID,
        payload: SectionUpdateRequest,
    ) -> JourneySectionResponse:
        await self._get_template(agent, template_id)
        section = await self.repo.get_section_in_template(template_id, section_id)
        if section is None:
            raise NotFoundError("Section not found.", code="journey.section_not_found")
        changes = payload.model_dump(exclude_unset=True)
        agency_default = await self.agency_default(agent.agency_id)
        if "name" in changes or "name_i18n" in changes:
            scalar, blob = apply_i18n_write(
                payload.name_i18n if "name_i18n" in changes else None,
                payload.name if "name" in changes else None,
                agency_default,
                section.name,
                section.name_i18n,
            )
            section.name = scalar or section.name
            section.name_i18n = blob
        if "description" in changes or "description_i18n" in changes:
            if "description" in changes and payload.description is None:
                section.description = None  # explicit clear
                section.description_i18n = normalize_i18n_input(payload.description_i18n)
            else:
                scalar, blob = apply_i18n_write(
                    payload.description_i18n if "description_i18n" in changes else None,
                    payload.description if "description" in changes else None,
                    agency_default,
                    section.description,
                    section.description_i18n,
                )
                section.description = scalar
                section.description_i18n = blob
        await self.db.commit()
        await self.db.refresh(section)
        return JourneySectionResponse.model_validate(section)

    async def reorder_sections(
        self, agent: Agent, template_id: uuid.UUID, section_ids: list[uuid.UUID]
    ) -> list[JourneySectionResponse]:
        """Full ordered set of the template's section ids (same convention
        as reorder_fields). A foreign/incomplete set → 422. Two-phase dense
        renumber to 0..n-1."""
        await self._get_template(agent, template_id)
        sections = await self.repo.list_sections(template_id)
        if len(section_ids) != len(set(section_ids)) or set(section_ids) != {
            s.id for s in sections
        }:
            raise ValidationError(
                "section_ids must contain exactly the template's sections, once each.",
                code="journey.section_order_mismatch",
            )
        offset = max((s.position for s in sections), default=-1) + len(sections) + 1
        await self.repo.shift_section_positions(template_id, offset)
        for index, section_id in enumerate(section_ids):
            await self.repo.set_section_position(section_id, index)
        await self.db.commit()
        reordered = await self.repo.list_sections(template_id)
        return [JourneySectionResponse.model_validate(s) for s in reordered]

    async def delete_section(
        self, agent: Agent, template_id: uuid.UUID, section_id: uuid.UUID
    ) -> None:
        """Delete a section. Its fields (both planes) fall back to the NULL
        bucket via ON DELETE SET NULL — declarations are never lost."""
        await self._get_template(agent, template_id)
        section = await self.repo.get_section_in_template(template_id, section_id)
        if section is None:
            raise NotFoundError("Section not found.", code="journey.section_not_found")
        await self.repo.delete_section(section)
        await self.db.commit()

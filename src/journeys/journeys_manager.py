import asyncio
import logging
import uuid
from collections import defaultdict

from fastapi import UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from shared.models.journey import (
    JourneyTemplate,
    JourneyTemplateField,
    JourneyTemplateStep,
)
from shared.models.step_case_requirement import StepCaseRequirement
from shared.models.step_requirement import StepRequirement
from src.cases.case_fields import COLLECTABLE_CASE_FIELDS
from src.core import storage
from src.core.config import get_settings
from src.core.enums import CompletionMode, StepRequirementKind, StepValidatorType
from src.core.exceptions import (
    ConflictError,
    NotFoundError,
    PayloadTooLargeError,
    ValidationError,
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
    StepRequirementCreateRequest,
    TemplateCaseFieldResponse,
    TemplateFieldCreateRequest,
    TemplateFieldResponse,
    TemplateFieldUpdateRequest,
    TemplateStepCreateRequest,
    TemplateStepResponse,
    TemplateStepUpdateRequest,
    UnsectionedFields,
)
from src.progress.requirements_eval import COLLECTABLE_BASE_FIELDS

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

    async def _get_template(self, agent: Agent, template_id: uuid.UUID) -> JourneyTemplate:
        template = await self.repo.get_template_in_agency(agent.agency_id, template_id)
        if template is None:
            raise NotFoundError("Journey template not found.")
        return template

    async def get_template_detail(
        self, agent: Agent, template_id: uuid.UUID
    ) -> JourneyTemplateDetailResponse:
        template = await self._get_template(agent, template_id)
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
        return JourneyTemplateDetailResponse(
            id=template.id,
            name=template.name,
            steps=[
                TemplateStepResponse(
                    id=step.id,
                    name=step.name,
                    position=step.position,
                    estimated_days=step.estimated_days,
                    default_responsible_type=step.default_responsible_type,
                    default_responsible_agent_id=step.default_responsible_agent_id,
                    completion_mode=step.completion_mode,
                    default_validated_by_type=step.default_validated_by_type,
                    default_validated_by_agent_id=step.default_validated_by_agent_id,
                    prerequisite_step_ids=by_step.get(step.id, []),
                    content_note=step.content_note,
                    attachments=attach_by_step.get(step.id, []),
                )
                for step in steps
            ],
            fields=field_resps,
            case_fields=case_resps,
            sections=[
                JourneySectionDetail(
                    id=s.id,
                    name=s.name,
                    description=s.description,
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

    async def create_template(self, agent: Agent, name: str) -> JourneyTemplate:
        template = self.repo.add_template(agent.agency_id, name)
        await self.db.commit()
        await self.db.refresh(template)
        return template

    async def update_template(
        self, agent: Agent, template_id: uuid.UUID, payload: JourneyTemplateUpdateRequest
    ) -> JourneyTemplate:
        template = await self._get_template(agent, template_id)
        if payload.name is not None:
            template.name = payload.name
        await self.db.commit()
        await self.db.refresh(template)
        return template

    async def delete_template(self, agent: Agent, template_id: uuid.UUID) -> None:
        template = await self._get_template(agent, template_id)
        assigned = await self.repo.count_cases_using_template(template_id)
        if assigned:
            # Clear 409, never the bare RESTRICT 500.
            raise ConflictError(
                f"Template is assigned to {assigned} case(s) and cannot be deleted."
            )
        await self.repo.delete_template(template)
        await self.db.commit()

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
            raise ValidationError("Default responsible must belong to this agency.")

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
        max_position = await self.repo.max_position(template_id)
        step = self.repo.add_step(
            template_id=template_id,
            name=payload.name,
            position=(max_position if max_position is not None else -1) + 1,
            estimated_days=payload.estimated_days,
            default_responsible_type=payload.default_responsible_type,
            default_responsible_agent_id=payload.default_responsible_agent_id,
            completion_mode=completion_mode,
            default_validated_by_type=validated_by_type,
            default_validated_by_agent_id=payload.default_validated_by_agent_id,
        )
        await self.db.flush()
        # Option-A backfill: on an ASSIGNED template, the new step is
        # instantiated on every live case (same transaction as the
        # step creation — atomic).
        from src.progress.progress_manager import ProgressManager

        await ProgressManager(self.db).backfill_step(agent, step)
        await self.db.commit()
        await self.db.refresh(step)
        return step

    async def _get_step(self, template_id: uuid.UUID, step_id: uuid.UUID) -> JourneyTemplateStep:
        step = await self.repo.get_step_in_template(template_id, step_id)
        if step is None:
            raise NotFoundError("Template step not found.")
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
                f"Template is assigned to {assigned} case(s); its steps cannot be deleted."
            )
        await self.repo.delete_step(step)
        await self.db.flush()
        await self._renumber_dense(template_id)
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
            raise ValidationError("A filename is required.")
        ext = original.rsplit(".", 1)[-1].lower() if "." in original else ""
        if ext not in settings.allowed_document_extensions:
            allowed = ", ".join(settings.allowed_document_extensions)
            raise ValidationError(f"File type not allowed (accepted: {allowed}).")
        content = await file.read()
        if len(content) > settings.max_document_size_mb * 1024 * 1024:
            raise PayloadTooLargeError(
                f"File exceeds the {settings.max_document_size_mb} MB limit."
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
            raise NotFoundError("Attachment not found.")
        content = await asyncio.to_thread(storage.download, row.storage_path)
        return row.filename, content

    async def delete_step_attachment(
        self, agent: Agent, template_id: uuid.UUID, step_id: uuid.UUID, attachment_id: uuid.UUID
    ) -> None:
        await self._get_template(agent, template_id)
        await self._get_step(template_id, step_id)
        row = await self.repo.get_step_attachment_in_step(step_id, attachment_id)
        if row is None:
            raise NotFoundError("Attachment not found.")
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
            raise ValidationError("step_ids must contain exactly the template's steps, once each.")
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
            raise ValidationError("A step cannot be its own prerequisite.")
        template_step_ids = {step.id for step in await self.repo.list_steps(template_id)}
        if not proposed <= template_step_ids:
            raise ValidationError("Prerequisites must belong to the same template.")

        # Full graph = existing edges of the other steps + the proposed
        # set for this one.
        graph: dict[uuid.UUID, set[uuid.UUID]] = {sid: set() for sid in template_step_ids}
        for row in await self.repo.list_prerequisites(template_id):
            if row.step_id != step_id:
                graph[row.step_id].add(row.prerequisite_step_id)
        graph[step_id] = proposed
        if _has_cycle(graph):
            raise ValidationError("This prerequisite change would create a cycle.")

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
        requirement = self.repo.add_requirement(
            step_id=step_id,
            kind=payload.kind.value,
            reference=payload.reference,
            scope=payload.scope.value,
            position=payload.position,
        )
        await self.db.commit()
        await self.db.refresh(requirement)
        return requirement

    async def _validate_reference(
        self, agent: Agent, kind: StepRequirementKind, reference: str
    ) -> None:
        """base_field → whitelist; custom_field → an ACTIVE definition of
        the agency must exist (a later archive is handled at read time
        via is_archived); document → free label, nothing to check."""
        if kind is StepRequirementKind.BASE_FIELD:
            if reference not in COLLECTABLE_BASE_FIELDS:
                raise ValidationError(
                    f"Unknown base field {reference!r}. Allowed: {sorted(COLLECTABLE_BASE_FIELDS)}."
                )
        elif kind is StepRequirementKind.CUSTOM_FIELD:
            definition = await CustomFieldsRepository(self.db).get_by_key(
                agent.agency_id, reference
            )
            if definition is None or definition.archived_at is not None:
                raise ValidationError(
                    f"No active custom field with key {reference!r} for this agency."
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
                "requirement_ids must contain exactly the step's requirements, once each."
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
            raise NotFoundError("Step requirement not found.")
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
                f"Allowed: {sorted(COLLECTABLE_CASE_FIELDS)}."
            )
        existing = await self.repo.get_step_case_requirement_by_ref(step_id, payload.case_field)
        if existing is not None:
            raise ConflictError(
                f"Case field {payload.case_field!r} is already required by this step."
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
                "case_requirement_ids must contain exactly the step's case requirements, once each."
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
            raise NotFoundError("Step case requirement not found.")
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
                "A creation field is a base_field or custom_field (documents are requirements)."
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
                f"Field {payload.reference!r} is already collected by this template."
            )
        field = self.repo.add_field(
            template_id=template_id,
            kind=payload.kind.value,
            reference=payload.reference,
            position=payload.position,
            required_at_creation=payload.required_at_creation,
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
                "field_ids must contain exactly the template's fields, once each."
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
            raise ValidationError("Section must belong to this template.")

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
            raise NotFoundError("Template field not found.")
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
            raise NotFoundError("Template field not found.")
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
                f"Allowed: {sorted(COLLECTABLE_CASE_FIELDS)}."
            )
        # Dedup pre-check → clean 409 (UNIQUE(template_id, case_field) is
        # the floor; this gives a friendly error).
        existing = await self.repo.get_case_field_by_ref(template_id, payload.case_field)
        if existing is not None:
            raise ConflictError(
                f"Case field {payload.case_field!r} is already collected by this template."
            )
        case_field = self.repo.add_case_field(
            template_id=template_id,
            case_field=payload.case_field,
            position=payload.position,
            required_at_creation=payload.required_at_creation,
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
                "case_field_ids must contain exactly the template's case fields, once each."
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
            raise NotFoundError("Template case field not found.")
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
            raise NotFoundError("Template case field not found.")
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
        max_position = await self.repo.max_section_position(template_id)
        section = self.repo.add_section(
            template_id=template_id,
            name=payload.name,
            description=payload.description,
            position=(max_position if max_position is not None else -1) + 1,
        )
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
            raise NotFoundError("Section not found.")
        changes = payload.model_dump(exclude_unset=True)
        if changes.get("name") is not None:
            section.name = changes["name"]
        if "description" in changes:
            section.description = changes["description"]
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
                "section_ids must contain exactly the template's sections, once each."
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
            raise NotFoundError("Section not found.")
        await self.repo.delete_section(section)
        await self.db.commit()

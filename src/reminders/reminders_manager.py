import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from shared.models.client_case import ClientCase
from shared.models.message_template import MessageTemplate
from shared.models.reminder import Reminder
from src.activity.activity_manager import ActivityManager
from src.core.enums import (
    ActorType,
    RecipientType,
    ReminderChannel,
    ReminderStatus,
    StepStatus,
)
from src.core.exceptions import ConflictError, NotFoundError, ValidationError
from src.reminders.reminder_tokens import (
    AGENCY_TOKENS,
    VARIABLE_PATTERN,
    agency_value,
    render_with_examples,
    unknown_tokens,
    used_tokens,
)
from src.reminders.reminders_repository import RemindersRepository
from src.reminders.reminders_schema import (
    MessageTemplateCreateRequest,
    MessageTemplateUpdateRequest,
    ReminderCreateRequest,
    ReminderPreviewRequest,
    ReminderPreviewResponse,
    ReminderResponse,
    ReminderUpdateRequest,
    UnresolvableToken,
)
from src.reminders.reminders_targeting import targeted_member
from src.usage.usage_manager import UsageManager

logger = logging.getLogger(__name__)

# A reminder pinned on a step that is DONE is FALSE, not merely late: its whole
# point is « votre étape n'a pas progressé ». The rule lives here, at the back,
# on EVERY path that leads to a send — unit approve (refuse, below), bulk
# approve (ignore + count), and the dispatch itself (skip + cancel, see
# reminders_jobs). The front already filters the selection; the back must not
# depend on it. Constat du 13/08: 7 of the 85 reminders waiting at
# domiciliation-bulgarie aimed at a step since completed.
_STEP_DONE_REFUSAL = (
    "This reminder targets a step that is already done — a follow-up saying the "
    "step has not progressed would be false. Unlink the step or cancel the reminder."
)


class RemindersManager:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.repo = RemindersRepository(db)
        self.activity = ActivityManager(db)

    def _log(
        self,
        case_id: uuid.UUID,
        agent: Agent,
        action_type: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.activity.log_action(
            case_id=case_id,
            actor_type=ActorType.AGENT,
            actor_id=agent.id,
            action_type=action_type,
            details=details,
        )

    # --- message templates ----------------------------------------------------------

    async def list_message_templates(self, agent: Agent) -> list[MessageTemplate]:
        return await self.repo.list_message_templates(agent.agency_id)

    async def create_message_template(
        self, agent: Agent, payload: MessageTemplateCreateRequest
    ) -> MessageTemplate:
        template = self.repo.add_message_template(
            agency_id=agent.agency_id,
            name=payload.name,
            body=payload.body,
            # Étiquettes de recherche, jamais des règles : un modèle marqué
            # « email » reste applicable à un WhatsApp.
            language=payload.language,
            channel=payload.channel.value if payload.channel is not None else None,
        )
        await self.db.commit()
        await self.db.refresh(template)
        return template

    async def update_message_template(
        self, agent: Agent, template_id: uuid.UUID, payload: MessageTemplateUpdateRequest
    ) -> MessageTemplate:
        template = await self.repo.get_message_template_in_agency(agent.agency_id, template_id)
        if template is None:
            raise NotFoundError("Message template not found.")
        # `exclude_unset` distingue ABSENT (inchangé) de `null` (effacé) —
        # c'est ce qui permet de RETIRER une étiquette. Mais `name` et `body`
        # sont NOT NULL : un `null` explicite sur eux passait la validation
        # et faisait tomber l'insert en 500. On l'ignore, comme une absence.
        patch = payload.model_dump(exclude_unset=True)
        for field, value in patch.items():
            if value is None and field in ("name", "body"):
                continue
            setattr(template, field, value)
        await self.db.commit()
        await self.db.refresh(template)
        return template

    async def delete_message_template(self, agent: Agent, template_id: uuid.UUID) -> None:
        template = await self.repo.get_message_template_in_agency(agent.agency_id, template_id)
        if template is None:
            raise NotFoundError("Message template not found.")
        await self.repo.delete_row(template)
        await self.db.commit()

    # --- interpolation (server-side, at creation/edition) ------------------------------

    async def _resolve_values(
        self,
        case: ClientCase,
        needed: set[str],
        step_progress_id: uuid.UUID | None,
        scheduled_at: datetime,
    ) -> tuple[dict[str, str], list[tuple[str, str]]]:
        """(valeurs résolues, [(jeton, raison)] non résolubles). NE LÈVE
        JAMAIS : le figeage lève à partir de ce résultat, l'aperçu le sert
        tel quel. UNE seule résolution pour les deux, donc l'aperçu ne peut
        pas flatter ce que le figeage produira — même doctrine que l'aperçu
        des conditions générales.

        Les raisons sont des slugs stables, pas des phrases : le front les
        traduit, il ne les affiche pas brutes."""
        values: dict[str, str] = {}
        failures: list[tuple[str, str]] = []

        if "client_name" in needed:
            principal = await self.repo.get_expat(case.principal_expat_user_id)
            assert principal is not None
            values["client_name"] = f"{principal.first_name} {principal.last_name}"

        if needed & {"step_name", "days_left"}:
            progress = (
                None
                if step_progress_id is None
                else await self.repo.get_progress_in_case(case.id, step_progress_id)
            )
            if progress is None:
                # L'ordre du catalogue décide de la variable nommée en
                # premier (step_name avant days_left) — verdict déterministe.
                for name in ("step_name", "days_left"):
                    if name in needed:
                        failures.append((name, "step_required"))
            else:
                template_step = await self.repo.get_template_step(progress.template_step_id)
                assert template_step is not None
                values["step_name"] = template_step.name
                if "days_left" in needed:
                    started_at = await self.repo.get_step_started_at(case.id, progress.id)
                    if template_step.estimated_days is None:
                        failures.append(("days_left", "estimated_days_required"))
                    elif started_at is None:
                        failures.append(("days_left", "step_not_started"))
                    else:
                        elapsed = (scheduled_at.date() - started_at.date()).days
                        values["days_left"] = str(max(0, template_step.estimated_days - elapsed))

        if needed & set(AGENCY_TOKENS):
            agency = await self.repo.get_agency(case.agency_id)
            assert agency is not None
            for name in AGENCY_TOKENS:
                if name not in needed:
                    continue
                value = agency_value(name, agency)
                if value is None:
                    # Un champ d'agence vide gèlerait un TROU dans un message
                    # envoyé une fois — même refus qu'une variable de dossier.
                    failures.append((name, "agency_field_empty"))
                else:
                    values[name] = value

        return values, failures

    async def _render(
        self,
        case: ClientCase,
        raw: str,
        step_progress_id: uuid.UUID | None,
        scheduled_at: datetime,
    ) -> str:
        """Freeze the variables into the approved text. {days_left} is
        PROJECTED AT scheduled_at (estimated_days − days between the
        step's start and the planned send date, floor 0) — the approver
        reads a text that is exact AT SEND TIME. Unsolvable variable →
        422 naming it.

        Un jeton INCONNU ({tva}) n'est pas une erreur ici : il traverse et se
        fige verbatim, comme dans les conditions. C'est l'aperçu qui le
        signale À L'ÉDITION (`preview_reminder`), avant que le texte ne soit
        pris — un message parti ne se rattrape pas."""
        needed = set(VARIABLE_PATTERN.findall(raw))
        if not needed:
            return raw
        values, failures = await self._resolve_values(case, needed, step_progress_id, scheduled_at)
        if failures:
            variable, reason = failures[0]
            raise ValidationError(
                f"{{{variable}}} cannot be resolved ({reason}).",
                code="reminder.variable_unresolvable",
                params={"variable": variable, "reason": reason},
            )

        rendered = raw
        for key, value in values.items():
            rendered = rendered.replace(f"{{{key}}}", value)
        return rendered

    async def preview_reminder(
        self, agent: Agent, payload: ReminderPreviewRequest
    ) -> ReminderPreviewResponse:
        """« Ce que votre client lira », calculé sur le BROUILLON — la MÊME
        résolution que le figeage, donc l'aperçu ne peut pas mentir.

        Avec un dossier en face : les vraies valeurs. Sans (un modèle de
        message, une modale encore vide) : les spécimens du catalogue. Les
        jetons inconnus sont nommés ici parce qu'ils seront GELÉS verbatim,
        et les non-résolubles parce qu'ils lèveraient un 422 au figeage — un
        refus ne doit jamais surprendre au moment d'enregistrer."""
        agency = await self.repo.get_agency(agent.agency_id)
        unknown = unknown_tokens(payload.content)
        needed = set(used_tokens(payload.content))

        values: dict[str, str] = {}
        failures: list[tuple[str, str]] = []
        if payload.case_id is not None and needed:
            case = await self.repo.get_case_in_agency(agent.agency_id, payload.case_id)
            if case is None:
                raise NotFoundError("Case not found.", code="case.not_found")
            values, failures = await self._resolve_values(
                case,
                needed,
                payload.step_progress_id,
                payload.scheduled_at or datetime.now(UTC),
            )

        # Les jetons résolus prennent leur VRAIE valeur ; ceux qui manquent
        # (pas de dossier, ou non résolubles) retombent sur leur spécimen,
        # pour que l'agence lise toujours une phrase entière et pas un trou.
        rendered = payload.content
        for name, value in values.items():
            rendered = rendered.replace("{" + name + "}", value)
        rendered = render_with_examples(rendered, agency)

        return ReminderPreviewResponse(
            rendered=rendered,
            unknown_tokens=unknown,
            unresolvable_tokens=[
                UnresolvableToken(token="{" + name + "}", name=name, reason=reason)
                for name, reason in failures
            ],
        )

    # --- reminder creation -----------------------------------------------------------------

    async def _validate_recipient(
        self,
        case: ClientCase,
        channel: ReminderChannel | str,
        recipient_type: RecipientType | str,
        recipient_external_id: uuid.UUID | None,
    ) -> None:
        recipient = RecipientType(recipient_type)
        channel_value = ReminderChannel(channel)
        if recipient is RecipientType.EXPAT:
            if recipient_external_id is not None:
                raise ValidationError(
                    "recipient_external_id must be empty for recipient_type 'expat'."
                )
            return
        if recipient_external_id is None:
            raise ValidationError(
                "recipient_external_id is required for recipient_type 'external'."
            )
        contact = await self.repo.get_external_contact_in_case(case.id, recipient_external_id)
        if contact is None:
            raise ValidationError("Recipient external contact must belong to this case.")
        if channel_value is ReminderChannel.MAIL and not contact.email:
            raise ValidationError("The external contact has no email address.")

    async def create_reminder(
        self, agent: Agent, case_id: uuid.UUID, payload: ReminderCreateRequest
    ) -> Reminder:
        case = await self.repo.get_case_in_agency(agent.agency_id, case_id)
        if case is None:
            raise NotFoundError("Case not found.")
        await self._validate_recipient(
            case, payload.channel, payload.recipient_type, payload.recipient_external_id
        )
        if payload.step_progress_id is not None and (
            await self.repo.get_progress_in_case(case.id, payload.step_progress_id) is None
        ):
            raise ValidationError("step_progress_id does not belong to this case.")

        if payload.message_template_id is not None:
            template = await self.repo.get_message_template_in_agency(
                agent.agency_id, payload.message_template_id
            )
            if template is None:
                raise ValidationError("Message template not found in this agency.")
            raw = template.body
        elif payload.message_body is not None:
            raw = payload.message_body
        else:
            raise ValidationError("Either message_template_id or message_body is required.")

        body = await self._render(case, raw, payload.step_progress_id, payload.scheduled_at)
        reminder = self.repo.add_reminder(
            case_id=case.id,
            step_progress_id=payload.step_progress_id,
            message_template_id=payload.message_template_id,
            channel=payload.channel.value,
            scheduled_at=payload.scheduled_at,
            recipient_type=payload.recipient_type.value,
            recipient_external_id=payload.recipient_external_id,
            message_body=body,
        )
        await self.db.flush()
        self._log(
            case.id,
            agent,
            "reminder.created",
            {"reminder_id": str(reminder.id), "channel": reminder.channel},
        )
        await UsageManager(self.db).emit_for_case(
            case, "reminder.scheduled", actor_type=ActorType.AGENT, actor_id=agent.id
        )
        await self.db.commit()
        await self.db.refresh(reminder)
        return reminder

    # --- read -------------------------------------------------------------------------------

    async def get_reminder(self, agent: Agent, reminder_id: uuid.UUID) -> Reminder:
        reminder = await self.repo.get_reminder_in_agency(agent.agency_id, reminder_id)
        if reminder is None:
            raise NotFoundError("Reminder not found.")
        return reminder

    async def _targets_a_done_step(self, reminder: Reminder) -> bool:
        """True when the reminder is pinned on a step already validated. A
        reminder without a linked step (a free note, a generic follow-up) is
        never concerned — nothing claims a step stalled."""
        if reminder.step_progress_id is None:
            return False
        progress = await self.repo.get_progress_in_case(reminder.case_id, reminder.step_progress_id)
        return progress is not None and progress.status == StepStatus.DONE.value

    async def list_reminders(
        self, agent: Agent, filters: dict[str, Any], page: int, page_size: int
    ) -> tuple[list[Reminder], int]:
        return await self.repo.list_reminders(agent.agency_id, filters, page, page_size)

    # --- the approval screen says the REAL recipient -------------------------------------

    async def to_response(self, reminder: Reminder) -> ReminderResponse:
        response = ReminderResponse.model_validate(reminder)
        response.resolved_recipient = await self._resolved_recipient(reminder)
        return response

    async def to_responses(self, reminders: list[Reminder]) -> list[ReminderResponse]:
        return [await self.to_response(reminder) for reminder in reminders]

    async def _resolved_recipient(self, reminder: Reminder) -> str | None:
        """Mirror of the dispatch routing (reminders_targeting), for
        DISPLAY: who will actually receive this reminder. Best-effort —
        a resolution hiccup shows None, never a 500 on the calendar."""
        try:
            if reminder.recipient_type == RecipientType.EXTERNAL.value:
                contact = (
                    await self.repo.get_external_contact_in_case(
                        reminder.case_id, reminder.recipient_external_id
                    )
                    if reminder.recipient_external_id
                    else None
                )
                return contact.name if contact is not None else None
            if reminder.recipient_type == RecipientType.AGENT.value:
                return await self.repo.get_owner_display(reminder.case_id)
            # EXPAT: the targeted member with access, else the principal.
            if reminder.step_progress_id is not None:
                requirements = await self.repo.list_step_requirements_for_progress(
                    reminder.step_progress_id
                )
                persons = await self.repo.persons_by_id_for_case(reminder.case_id)
                person = targeted_member(requirements, persons)
                if person is not None and person.expat_user_id is not None:
                    member = await self.repo.get_expat(person.expat_user_id)
                    if member is not None and member.email:
                        return person.full_name or f"{member.first_name} {member.last_name}"
            return await self.repo.get_principal_display(reminder.case_id)
        except Exception:  # noqa: BLE001 — display data, never a 500
            logger.exception("reminder recipient resolution failed for %s", reminder.id)
            return None

    # --- state machine -------------------------------------------------------------------------

    async def update_reminder(
        self, agent: Agent, reminder_id: uuid.UUID, payload: ReminderUpdateRequest
    ) -> Reminder:
        reminder = await self.get_reminder(agent, reminder_id)
        if reminder.status not in (
            ReminderStatus.TO_APPROVE.value,
            ReminderStatus.APPROVED.value,
        ):
            raise ConflictError("Only to_approve or approved reminders can be edited.")
        case = await self.repo.get_case_in_agency(agent.agency_id, reminder.case_id)
        assert case is not None

        data = payload.model_dump(exclude_unset=True)
        was_approved = reminder.status == ReminderStatus.APPROVED.value

        new_channel = data.get("channel", ReminderChannel(reminder.channel))
        new_recipient_type = data.get("recipient_type", RecipientType(reminder.recipient_type))
        new_external_id = data.get("recipient_external_id", reminder.recipient_external_id)
        await self._validate_recipient(case, new_channel, new_recipient_type, new_external_id)

        new_step_id = data.get("step_progress_id", reminder.step_progress_id)
        if new_step_id is not None and (
            await self.repo.get_progress_in_case(case.id, new_step_id) is None
        ):
            raise ValidationError("step_progress_id does not belong to this case.")

        new_scheduled_at = data.get("scheduled_at", reminder.scheduled_at)

        # Re-render source: an explicit body wins; else the (possibly
        # updated) linked template; else the stored body — for free-text
        # reminders its variables are already frozen, re-rendering is a
        # no-op (re-provide the body to refresh them).
        new_template_id = data.get("message_template_id", reminder.message_template_id)
        if "message_body" in data and data["message_body"] is not None:
            raw = data["message_body"]
        elif new_template_id is not None:
            template = await self.repo.get_message_template_in_agency(
                agent.agency_id, new_template_id
            )
            if template is None:
                raise ValidationError("Message template not found in this agency.")
            raw = template.body
        else:
            raw = reminder.message_body

        reminder.channel = ReminderChannel(new_channel).value
        reminder.scheduled_at = new_scheduled_at
        reminder.recipient_type = RecipientType(new_recipient_type).value
        reminder.recipient_external_id = new_external_id
        reminder.step_progress_id = new_step_id
        reminder.message_template_id = new_template_id
        reminder.message_body = await self._render(case, raw, new_step_id, new_scheduled_at)

        if was_approved:
            # The approval covered the OLD content — re-approve.
            reminder.status = ReminderStatus.TO_APPROVE.value
            reminder.approved_by_agent_id = None
        self._log(
            case.id,
            agent,
            "reminder.edited",
            {"reminder_id": str(reminder.id), "reapproval_required": was_approved},
        )
        await self.db.commit()
        await self.db.refresh(reminder)
        return reminder

    async def approve_reminder(self, agent: Agent, reminder_id: uuid.UUID) -> Reminder:
        reminder = await self.get_reminder(agent, reminder_id)
        if reminder.status != ReminderStatus.TO_APPROVE.value:
            raise ConflictError("Only to_approve reminders can be approved.")
        # One reminder, one explicit gesture → an explicit answer (409). In
        # bulk the same rule ignores instead, and says how many (the batch of
        # 85 must not fail because 7 of its steps got validated meanwhile).
        if await self._targets_a_done_step(reminder):
            raise ConflictError(_STEP_DONE_REFUSAL)
        reminder.status = ReminderStatus.APPROVED.value
        reminder.approved_by_agent_id = agent.id
        self._log(
            reminder.case_id,
            agent,
            "reminder.approved",
            {"reminder_id": str(reminder.id), "approved_by": str(agent.id)},
        )
        await self.db.commit()
        await self.db.refresh(reminder)
        return reminder

    async def bulk_approve(
        self, agent: Agent, reminder_ids: list[uuid.UUID]
    ) -> tuple[int, int, int]:
        """Approve a batch in ONE gesture — the mirror of bulk_cancel for the
        agency that reads its backlog and wants it to LEAVE. Same bounds, same
        silence on what is not approvable (another agency's ids, a reminder
        already approved / sent / cancelled): `affected` says what moved.

        THE ONE ASYMMETRY: a reminder whose target step is DONE is NOT
        approved. It is counted apart in `skipped_step_done` — ignored, never
        in silence — because a batch of 85 must not be rejected wholesale for
        the 7 whose step got validated in the meantime. They stay TO_APPROVE:
        bulk-cancel is the gesture that clears them.

        Each approval is logged exactly like the unit one — the trace stays
        per-reminder, no « 85 rappels sont partis » hole in the history."""
        rows = await self.repo.list_bulk_targets_in_agency(
            agent.agency_id, reminder_ids, [ReminderStatus.TO_APPROVE.value]
        )
        done_steps = await self.repo.done_progress_ids(
            [r.step_progress_id for r in rows if r.step_progress_id is not None]
        )
        approved = 0
        skipped_step_done = 0
        for reminder in rows:
            if reminder.step_progress_id in done_steps:
                skipped_step_done += 1
                continue
            reminder.status = ReminderStatus.APPROVED.value
            reminder.approved_by_agent_id = agent.id
            self._log(
                reminder.case_id,
                agent,
                "reminder.approved",
                {"reminder_id": str(reminder.id), "approved_by": str(agent.id), "bulk": True},
            )
            approved += 1
        await self.db.commit()
        return len(reminder_ids), approved, skipped_step_done

    async def bulk_cancel(self, agent: Agent, reminder_ids: list[uuid.UUID]) -> tuple[int, int]:
        """Cancel a batch in ONE gesture — the way out of an approval backlog
        that piled up (97 rows in prod at the 13/08 constat, the oldest 17 days
        old). Only this agency's reminders, only cancellable ones; everything
        else is silently ignored, so `affected` may be lower than `examined`.

        Each cancellation is logged exactly like the unit one: the trace stays
        per-reminder, no « 85 rappels ont disparu » hole in the history."""
        rows = await self.repo.list_bulk_targets_in_agency(
            agent.agency_id,
            reminder_ids,
            [ReminderStatus.TO_APPROVE.value, ReminderStatus.APPROVED.value],
        )
        for reminder in rows:
            reminder.status = ReminderStatus.CANCELLED.value
            self._log(
                reminder.case_id,
                agent,
                "reminder.cancelled",
                {"reminder_id": str(reminder.id), "bulk": True},
            )
        await self.db.commit()
        return len(reminder_ids), len(rows)

    async def cancel_reminder(self, agent: Agent, reminder_id: uuid.UUID) -> Reminder:
        reminder = await self.get_reminder(agent, reminder_id)
        if reminder.status not in (
            ReminderStatus.TO_APPROVE.value,
            ReminderStatus.APPROVED.value,
        ):
            raise ConflictError("Only to_approve or approved reminders can be cancelled.")
        reminder.status = ReminderStatus.CANCELLED.value
        self._log(reminder.case_id, agent, "reminder.cancelled", {"reminder_id": str(reminder.id)})
        await self.db.commit()
        await self.db.refresh(reminder)
        return reminder

    async def mark_sent(self, agent: Agent, reminder_id: uuid.UUID) -> Reminder:
        """WhatsApp ONLY: the dispatcher never auto-sends this channel;
        the agent copies the rendered text, pastes it in WhatsApp, then
        confirms here. A GET never mutates."""
        reminder = await self.get_reminder(agent, reminder_id)
        if reminder.channel != ReminderChannel.WHATSAPP.value:
            raise ValidationError("mark-sent is only for the whatsapp channel.")
        if reminder.status != ReminderStatus.APPROVED.value:
            raise ConflictError("Only approved reminders can be marked sent.")
        reminder.status = ReminderStatus.SENT.value
        self._log(
            reminder.case_id,
            agent,
            "reminder.sent",
            {"reminder_id": str(reminder.id), "channel": reminder.channel, "manual": True},
        )
        await self.db.commit()
        await self.db.refresh(reminder)
        return reminder

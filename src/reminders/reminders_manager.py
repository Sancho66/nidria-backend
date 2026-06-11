import re
import uuid
from datetime import datetime
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
)
from src.core.exceptions import ConflictError, NotFoundError, ValidationError
from src.reminders.reminders_repository import RemindersRepository
from src.reminders.reminders_schema import (
    MessageTemplateCreateRequest,
    MessageTemplateUpdateRequest,
    ReminderCreateRequest,
    ReminderUpdateRequest,
)

_VARIABLE_PATTERN = re.compile(r"\{(client_name|step_name|days_left)\}")


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
            agency_id=agent.agency_id, name=payload.name, body=payload.body
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
        for field, value in payload.model_dump(exclude_unset=True).items():
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
        422 naming it."""
        needed = set(_VARIABLE_PATTERN.findall(raw))
        if not needed:
            return raw
        values: dict[str, str] = {}

        if "client_name" in needed:
            principal = await self.repo.get_expat(case.principal_expat_user_id)
            assert principal is not None
            values["client_name"] = f"{principal.first_name} {principal.last_name}"

        if needed & {"step_name", "days_left"}:
            if step_progress_id is None:
                variable = "step_name" if "step_name" in needed else "days_left"
                raise ValidationError(f"{{{variable}}} requires a linked step (step_progress_id).")
            progress = await self.repo.get_progress_in_case(case.id, step_progress_id)
            assert progress is not None  # validated by callers
            template_step = await self.repo.get_template_step(progress.template_step_id)
            assert template_step is not None
            values["step_name"] = template_step.name
            if "days_left" in needed:
                if template_step.estimated_days is None:
                    raise ValidationError(
                        "{days_left} requires the linked step to have estimated_days."
                    )
                started_at = await self.repo.get_step_started_at(case.id, progress.id)
                if started_at is None:
                    raise ValidationError("{days_left} requires the linked step to be started.")
                elapsed = (scheduled_at.date() - started_at.date()).days
                values["days_left"] = str(max(0, template_step.estimated_days - elapsed))

        rendered = raw
        for key, value in values.items():
            rendered = rendered.replace(f"{{{key}}}", value)
        return rendered

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
        await self.db.commit()
        await self.db.refresh(reminder)
        return reminder

    # --- read -------------------------------------------------------------------------------

    async def get_reminder(self, agent: Agent, reminder_id: uuid.UUID) -> Reminder:
        reminder = await self.repo.get_reminder_in_agency(agent.agency_id, reminder_id)
        if reminder is None:
            raise NotFoundError("Reminder not found.")
        return reminder

    async def list_reminders(
        self, agent: Agent, filters: dict[str, Any], page: int, page_size: int
    ) -> tuple[list[Reminder], int]:
        return await self.repo.list_reminders(agent.agency_id, filters, page, page_size)

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

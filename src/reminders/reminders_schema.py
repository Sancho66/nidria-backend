import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from src.core.enums import RecipientType, ReminderChannel


class MessageTemplateCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1)


class MessageTemplateUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    body: str | None = Field(default=None, min_length=1)


class MessageTemplateResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    body: str


class ReminderCreateRequest(BaseModel):
    """Body source: message_template_id OR free-text message_body (at
    least one). Variables are interpolated SERVER-side at creation —
    the approver reads the exact final text."""

    channel: ReminderChannel
    scheduled_at: datetime
    recipient_type: RecipientType
    recipient_external_id: uuid.UUID | None = None
    message_template_id: uuid.UUID | None = None
    message_body: str | None = Field(default=None, min_length=1)
    step_progress_id: uuid.UUID | None = None


class ReminderUpdateRequest(BaseModel):
    """Any edit of an APPROVED reminder sends it back to TO_APPROVE —
    the approval covers exactly what goes out."""

    channel: ReminderChannel | None = None
    scheduled_at: datetime | None = None
    recipient_type: RecipientType | None = None
    recipient_external_id: uuid.UUID | None = None
    message_template_id: uuid.UUID | None = None
    message_body: str | None = Field(default=None, min_length=1)
    step_progress_id: uuid.UUID | None = None


class ReminderResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    case_id: uuid.UUID
    step_progress_id: uuid.UUID | None
    message_template_id: uuid.UUID | None
    channel: str
    scheduled_at: datetime
    status: str
    recipient_type: str
    recipient_external_id: uuid.UUID | None
    message_body: str
    approved_by_agent_id: uuid.UUID | None
    auto_threshold_days: int | None
    # The REAL recipient the dispatch will resolve ("sera envoye a Claire
    # Martin") — routing 2026-07-18: an EXPAT reminder whose step targets
    # one member with access goes to HER; the approval screen must say it.
    # Display name (member full_name, principal name, contact name, owner
    # email) — None only when nothing is resolvable (defensive).
    resolved_recipient: str | None = None
    created_at: datetime
    updated_at: datetime


class ReminderListResponse(BaseModel):
    items: list[ReminderResponse]
    total: int
    page: int
    page_size: int

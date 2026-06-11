"""Expat portal field contract — designed BY EXCLUSION (what the client
must NOT see): no notes (even non-confidential — internal use), no raw
ActivityLog (the journal is the agency's tool; the projected timeline IS
the client view), no tags/source (internal qualification), no agent
list / internal staffing, NO internal UUID in the timeline. FR labels
("votre agence", "vous") are the frontend's job — the API ships stable
semantics."""

import uuid
from datetime import datetime

from pydantic import BaseModel


class ExpatAgencyResponse(BaseModel):
    name: str


class ExpatCaseSummaryResponse(BaseModel):
    id: uuid.UUID
    agency: ExpatAgencyResponse
    origin_country: str | None
    dest_country: str | None
    status: str
    steps_done: int
    steps_total: int
    created_at: datetime
    updated_at: datetime


class ExpatReferentResponse(BaseModel):
    """The named human contact at the agency (the case owner) —
    the brief: « nom du référent + son mail »."""

    first_name: str
    last_name: str
    email: str


class ExpatResponsibleResponse(BaseModel):
    """Displayable responsible: type 'agency' (no internal agent name —
    staffing doesn't face the client), 'you', or 'external' with the
    contact's name. type None = not assigned yet."""

    type: str | None
    name: str | None


class ExpatTimelineStepResponse(BaseModel):
    name: str
    position: int
    status: str  # projected (blocked computed at read time)
    estimated_days: int | None
    completed_at: datetime | None
    blocked_by: list[str]  # step NAMES, never ids
    responsible: ExpatResponsibleResponse
    # Step 15: the pieces the agency expects here ("documents attendus")
    # — free labels, informative.
    required_documents: list[str]


class ExpatCaseDetailResponse(ExpatCaseSummaryResponse):
    referent: ExpatReferentResponse | None
    timeline: list[ExpatTimelineStepResponse]


class ExpatNotificationResponse(BaseModel):
    """A SENT in_app reminder IS the notification (Q8 — no extra table).
    Its own id stays: the V1.5 mark-as-read will need it."""

    id: uuid.UUID
    message_body: str
    sent_at: datetime

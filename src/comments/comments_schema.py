import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class CommentCreateRequest(BaseModel):
    body: str = Field(min_length=1, max_length=5000)


class CommentUpdateRequest(BaseModel):
    body: str = Field(min_length=1, max_length=5000)


class CommentResponse(BaseModel):
    """One message in a step thread, shaped identically for both faces.
    No raw `author_id` is exposed (respects the expat exclusion contract /
    no internal UUID): `author_label` carries the resolved display name
    and `is_mine` (computed from the authenticated identity) drives the
    edit/delete affordances. `body` is null for a soft-deleted message
    (the row stays so the thread keeps its order — front renders
    "message supprimé")."""

    id: uuid.UUID
    author_type: str  # agent | expat
    author_label: str  # agent first name / client first+last — resolved, never an id
    is_mine: bool
    body: str | None
    edited: bool
    deleted: bool
    created_at: datetime
    updated_at: datetime

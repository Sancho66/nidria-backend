"""The Email module's platform switch for one agency, and its Lemlist team.

Two locks open the module (N3): the env master switch
`EMAIL_MODULE_ENABLED`, and this row's `enabled`, posed by a superadmin.
The switch lives in its own table on purpose, never in `agency.settings`:
`PATCH /agencies/me` (agency.manage) replaces that whole JSONB, so an
agency admin could otherwise switch the module on alone and use the
platform's Lemlist team.

A Lemlist team serves ONE agency (N4): lead deduplication, the unsubscribe
list, the sending mailboxes and the webhook are team-wide, so a shared team
would leak one agency's clients to another. Hence the partial unique index
on `(provider, provider_team_id)`. An agency without a team runs in dry run.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, text
from sqlalchemy.orm import Mapped, mapped_column

from shared.models.base import Base, TimestampMixin
from shared.models.email_campaign import PROVIDER_LEMLIST


class AgencyEmailModule(TimestampMixin, Base):
    __tablename__ = "agency_email_module"
    __table_args__ = (
        Index(
            "uq_agency_email_module_team",
            "provider",
            "provider_team_id",
            unique=True,
            postgresql_where=text("provider_team_id IS NOT NULL"),
        ),
    )

    # One row per agency at most: the agency IS the key.
    agency_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agency.id", ondelete="CASCADE"), primary_key=True
    )
    enabled: Mapped[bool] = mapped_column(
        nullable=False, default=False, server_default=text("false")
    )
    provider: Mapped[str] = mapped_column(
        String(30), nullable=False, default=PROVIDER_LEMLIST, server_default=PROVIDER_LEMLIST
    )
    # The Lemlist team bound to this agency (`_id` of `GET /team`), NULL = none.
    provider_team_id: Mapped[str | None] = mapped_column(String(64))
    # Shown to the superadmin only.
    provider_team_name: Mapped[str | None] = mapped_column(String(200))
    # The last time the switch was turned on, and off.
    enabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # The last superadmin who changed this row's state (switch or team).
    changed_by_agent_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("agent.id", ondelete="SET NULL")
    )

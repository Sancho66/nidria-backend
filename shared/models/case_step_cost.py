import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import Date, ForeignKey, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from shared.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class CaseStepCost(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """An agency-INTERNAL cost line on a case step (débours, frais spéciaux).

    The THIRD nature of a step — what the agency NOTES FOR ITSELF — beside what
    it PROVIDES to the client (content_note, attachments) and what it ASKS of
    the client (requirements). It is STRUCTURALLY absent from every expat and
    external projection: it lives in its OWN table, queried only by the
    agency-facing costs manager — never by expat_schema nor external_schema.

    Several lines per step (a comptable notes "timbre fiscal 120, notaire 180",
    not "300"). `amount` is DECIMAL(18,4) — never a float; the agency currency
    drives the DISPLAYED decimals (guaraní 0, euro 2). The case total is a SUM
    computed at read, never stored (a stored total lies the day a line moves).
    `author_agent_id` + created/updated_at trace who noted what, when; a line is
    correctable and deletable (a notebook, not legal accounting). The mutation
    history lives in activity_log (cost.added / cost.edited / cost.deleted)."""

    __tablename__ = "case_step_cost"

    case_step_progress_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("case_step_progress.id", ondelete="CASCADE"), index=True, nullable=False
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    label: Mapped[str] = mapped_column(String(200), nullable=False)
    incurred_on: Mapped[date | None] = mapped_column(Date)
    # SET NULL: a removed agent must not delete the financial line they noted.
    author_agent_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("agent.id", ondelete="SET NULL")
    )

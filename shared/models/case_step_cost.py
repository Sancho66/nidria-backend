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

    A line carries PLANNED and REAL side by side, EACH in its OWN currency:
    - `planned_amount` / `planned_currency` — what the template FORECAST, frozen
      by value at instantiation (both NULL for an unforeseen manual débours).
    - `amount` / `currency` — the REAL sum and the currency it was actually PAID
      in. `amount` is EMPTY until paid (NULL); `currency` (NOT NULL) starts equal
      to `planned_currency` (inherited) and the agency changes it if it paid in
      another money — NO conversion, ever: a rate would be a fabricated number.
    - `source_template_cost_id` — a TRACE back to the template's planned line, a
      dead reference (SET NULL): editing/deleting the template cost never touches
      this row, and the frozen planned_amount survives the template's deletion.

    Several lines per step (a comptable notes "timbre fiscal 120, notaire 180",
    not "300"). Amounts are DECIMAL(18,4) — never a float; the LINE's currency
    drives the accepted decimals (guaraní 0, euro 2) and the écart is only defined
    when planned and real share a currency. Totals are computed at read, GROUPED
    by currency, never summed across currencies and never stored. `author_agent_id`
    + created/updated_at trace who noted what, when; a line is correctable and
    deletable. Mutation history lives in activity_log (cost.added/edited/deleted)."""

    __tablename__ = "case_step_cost"

    case_step_progress_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("case_step_progress.id", ondelete="CASCADE"), index=True, nullable=False
    )
    # REAL amount — NULL until the agency pays (a planned line starts empty).
    amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    # Currency the REAL amount was paid in (ISO 4217, filtered catalogue). NOT
    # NULL: inherited from planned_currency at instantiation, editable when the
    # agency paid in another money. Drives the accepted decimals of `amount`.
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    # PLANNED amount, frozen by value at instantiation — NULL for a manual
    # débours nobody forecast. Never re-derived from the template (no propagation).
    planned_amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    # Currency of planned_amount, frozen from the template cost — NULL for a
    # manual débours. When it differs from `currency`, the line has no écart.
    planned_currency: Mapped[str | None] = mapped_column(String(3))
    label: Mapped[str] = mapped_column(String(200), nullable=False)
    incurred_on: Mapped[date | None] = mapped_column(Date)
    # SET NULL: a removed agent must not delete the financial line they noted.
    author_agent_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("agent.id", ondelete="SET NULL")
    )
    # Trace to the template planned cost this line was born from — a dead
    # reference, NOT a live link: SET NULL when the template cost is deleted,
    # and planned_amount (a copied value) survives untouched. NULL for a manual
    # débours (no origin).
    source_template_cost_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("journey_step_cost.id", ondelete="SET NULL")
    )

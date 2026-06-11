import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from shared.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class JobConfig(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Scheduled job configuration — ported from Prism, adapted:
    UUID PK, and PLATFORM-scoped (no agency_id): the MVP jobs
    (dispatch_reminders, auto_reminders) serve every agency; the
    per-agency toggle lives in agency.settings. The cron lives in
    DATA — editable at runtime (hot-reload), no redeploy."""

    __tablename__ = "job_config"

    job_id: Mapped[str] = mapped_column(String(100), unique=True, index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    cron_expression: Mapped[str] = mapped_column(String(100), nullable=False)
    timezone: Mapped[str] = mapped_column(
        String(50), default="UTC", server_default="UTC", nullable=False
    )
    is_enabled: Mapped[bool] = mapped_column(default=True, nullable=False)
    paused_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    starts_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_run_status: Mapped[str | None] = mapped_column(String(20))
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)


class JobRun(UUIDPrimaryKeyMixin, Base):
    """One row per job execution (incl. SKIPPED when disabled/paused).

    KNOWN LIMITATION (assumed for MVP, V1.5 sweep): the RUNNING row is
    committed upfront so operators can watch progress — a process crash
    mid-run leaves an orphan RUNNING row."""

    __tablename__ = "job_run"

    job_config_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("job_config.id", ondelete="CASCADE"), index=True, nullable=False
    )
    job_id: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_seconds: Mapped[int | None] = mapped_column(Integer)
    stats: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    error: Mapped[str | None] = mapped_column(Text)
    log_output: Mapped[str] = mapped_column(Text, default="", nullable=False)
    triggered_by: Mapped[str] = mapped_column(String(20), nullable=False)
    triggered_by_agent_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("agent.id", ondelete="SET NULL")
    )

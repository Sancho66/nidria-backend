"""Private sandbox queue, intentionally outside production Base metadata.

Only the isolated seed operator creates these tables. No production migration
or public sandbox handler exposes their prospect data.
"""

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID

metadata = MetaData()
jobs = Table(
    "demo_import_job",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("request_id", UUID(as_uuid=True), nullable=False, unique=True),
    Column("actor_id", UUID(as_uuid=True), nullable=False),
    Column("fingerprint", String(64), nullable=False),
    Column("batch", String(100), nullable=False),
    Column("lifetime_days", Integer, nullable=False),
    Column("status", String(20), nullable=False),
    Column("total", Integer, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)
rows = Table(
    "demo_import_row",
    metadata,
    Column(
        "job_id",
        UUID(as_uuid=True),
        ForeignKey("demo_import_job.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("row_number", Integer, primary_key=True),
    Column("data", JSONB, nullable=False),
    Column("status", String(20), nullable=False),
    Column("reused", Boolean, nullable=False, default=False),
    Column("result", JSONB, nullable=False),
    Column("errors", JSONB, nullable=False),
)
Index("ix_demo_import_pending", rows.c.job_id, rows.c.status, rows.c.row_number)
Index("ix_demo_import_jobs_status", jobs.c.status, jobs.c.created_at)

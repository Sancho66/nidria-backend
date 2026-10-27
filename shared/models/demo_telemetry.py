"""Isolated demo measurements, outside the production migration metadata."""

from sqlalchemy import Column, Date, DateTime, Index, Integer, MetaData, String, Table, func

metadata = MetaData()
measurement = Table(
    "demo_measurement",
    metadata,
    Column("key", String(40), primary_key=True),
    Column("started_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)
visits = Table(
    "demo_telemetry_visit",
    metadata,
    Column("code", String(32), primary_key=True),
    Column("visit_hash", String(64), primary_key=True),
    Column("day", Date, primary_key=True),
    Column("first_at", DateTime(timezone=True), nullable=False),
    Column("last_at", DateTime(timezone=True), nullable=False),
    Column("excluded_reason", String(30)),
    Column("tour_started_at", DateTime(timezone=True)),
    Column("tour_completed_at", DateTime(timezone=True)),
    Column("trial_clicked_at", DateTime(timezone=True)),
    Column("last_step", Integer, nullable=False),
)
Index("ix_demo_telemetry_day", visits.c.day, visits.c.code)

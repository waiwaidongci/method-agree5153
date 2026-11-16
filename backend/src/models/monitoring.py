from datetime import date, datetime
from sqlalchemy import Boolean, Date, DateTime, Float, ForeignKey, Integer, JSON, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from src.config.database import Base


class MonitoringProfile(Base):
    """A frozen baseline study plus the daily batches compared against it."""

    __tablename__ = "monitoring_profiles"
    __table_args__ = (UniqueConstraint("baseline_run_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(160), unique=True)
    baseline_run_id: Mapped[int] = mapped_column(ForeignKey("comparison_runs.id"))
    consecutive_limit: Mapped[int] = mapped_column(Integer, default=3)
    baseline: Mapped[dict] = mapped_column(JSON, default=dict)
    note: Mapped[str] = mapped_column(String(1000), default="")
    created_by: Mapped[str] = mapped_column(String(160), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    change_seq: Mapped[int] = mapped_column(Integer, default=0)
    pending_from: Mapped[date | None] = mapped_column(Date, nullable=True)


class MonitoringBatch(Base):
    """Current revision of one independently sampled comparison batch."""

    __tablename__ = "monitoring_batches"
    __table_args__ = (UniqueConstraint("monitor_id", "batch_key"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    monitor_id: Mapped[int] = mapped_column(ForeignKey("monitoring_profiles.id"))
    batch_key: Mapped[str] = mapped_column(String(80))
    sampling_date: Mapped[date] = mapped_column(Date)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    content_hash: Mapped[str] = mapped_column(String(64))
    row_count: Mapped[int] = mapped_column(Integer, default=0)
    rows: Mapped[list] = mapped_column(JSON, default=list)
    source_file: Mapped[str] = mapped_column(String(180), default="")
    note: Mapped[str] = mapped_column(String(600), default="")
    received_by: Mapped[str] = mapped_column(String(160), default="")
    first_received_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class MonitoringBatchRevision(Base):
    """Immutable history of every accepted batch revision (duplicates are not written)."""

    __tablename__ = "monitoring_batch_revisions"
    __table_args__ = (UniqueConstraint("batch_id", "revision"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("monitoring_batches.id"))
    revision: Mapped[int] = mapped_column(Integer)
    sampling_date: Mapped[date] = mapped_column(Date)
    content_hash: Mapped[str] = mapped_column(String(64))
    row_count: Mapped[int] = mapped_column(Integer)
    rows: Mapped[list] = mapped_column(JSON, default=list)
    source_file: Mapped[str] = mapped_column(String(180), default="")
    note: Mapped[str] = mapped_column(String(600), default="")
    accepted_by: Mapped[str] = mapped_column(String(160), default="")
    received_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class MonitoringEvaluation(Base):
    """A versioned, deterministic replay of the monitoring series."""

    __tablename__ = "monitoring_evaluations"
    __table_args__ = (UniqueConstraint("monitor_id", "version"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    monitor_id: Mapped[int] = mapped_column(ForeignKey("monitoring_profiles.id"))
    version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), default="queued")
    replay_from: Mapped[date | None] = mapped_column(Date, nullable=True)
    reason: Mapped[str] = mapped_column(String(300), default="")
    requested_by: Mapped[str] = mapped_column(String(160), default="")
    requested_seq: Mapped[int] = mapped_column(Integer, default=0)
    applied_seq: Mapped[int] = mapped_column(Integer, default=0)
    attempts: Mapped[int] = mapped_column(Integer, default=1)
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    fingerprint: Mapped[str] = mapped_column(String(64), default="")
    error: Mapped[str] = mapped_column(String(600), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

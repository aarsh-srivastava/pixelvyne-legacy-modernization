"""Platform tables. Every schema change needs an Alembic revision:

    docker compose run --rm api alembic revision --autogenerate -m "what changed"
    docker compose run --rm api alembic upgrade head

Read the generated file before committing: autogenerate misses renames.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, Uuid, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.base import Base


class Project(Base):
    """One legacy system being migrated."""

    __tablename__ = "projects"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(120))
    source_dialect: Mapped[str] = mapped_column(String(30), default="oracle")
    target_dialect: Mapped[str] = mapped_column(String(30), default="postgresql")
    status: Mapped[str] = mapped_column(String(30), default="created")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    files: Mapped[list[SourceFile]] = relationship(
        back_populates="project", cascade="all, delete-orphan", order_by="SourceFile.path"
    )


class SourceFile(Base):
    """An uploaded legacy script. Re-uploading the same path replaces its content."""

    __tablename__ = "source_files"
    __table_args__ = (UniqueConstraint("project_id", "path", name="uq_source_files_project_path"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    path: Mapped[str] = mapped_column(String(500))
    size_bytes: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64))
    content: Mapped[str] = mapped_column(Text, deferred=True)   # not loaded in listings
    uploaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    project: Mapped[Project] = relationship(back_populates="files")


class Job(Base):
    """A background task. Its id is also the Celery task id.

    Postgres, not Redis, is the source of truth for job status, so status survives
    a Redis restart and can be queried with SQL.
    """

    __tablename__ = "jobs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=True
    )
    kind: Mapped[str] = mapped_column(String(30))              # ping | analyse | migrate | validate
    status: Mapped[str] = mapped_column(String(30), default="queued")
    # queued | running | awaiting_approval | succeeded | failed
    stage: Mapped[str | None] = mapped_column(String(200), nullable=True)
    progress: Mapped[int] = mapped_column(Integer, default=0)  # 0-100
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

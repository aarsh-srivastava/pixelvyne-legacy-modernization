"""Request/response models for the REST API (what the frontend sees)."""

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    source_dialect: Literal["oracle"] = "oracle"
    target_dialect: Literal["postgresql"] = "postgresql"


class ProjectOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    source_dialect: str
    target_dialect: str
    status: str
    created_at: datetime
    file_count: int = 0


class SourceFileOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    path: str
    size_bytes: int
    sha256: str
    uploaded_at: datetime


class SourceFileDetail(SourceFileOut):
    content: str


class JobCreated(BaseModel):
    job_id: uuid.UUID


class JobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID | None
    kind: str
    status: str
    stage: str | None
    progress: int
    result: dict[str, Any] | None
    error: str | None
    created_at: datetime
    updated_at: datetime

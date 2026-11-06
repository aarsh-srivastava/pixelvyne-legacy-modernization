"""Importing this package registers every model on Base.metadata (Alembic needs that)."""

from app.database.base import Base
from app.database import models  # noqa: F401

__all__ = ["Base", "models"]

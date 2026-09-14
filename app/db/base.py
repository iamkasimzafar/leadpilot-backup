"""Declarative base and shared column mixins.

This module must not import any model: models import Base from here, so an
import in the other direction creates a cycle. The model registry that Alembic
needs lives in app/db/registry.py instead.
"""

import re
from datetime import datetime

from sqlalchemy import DateTime, func
from sqlalchemy.orm import DeclarativeBase, Mapped, declared_attr, mapped_column


class Base(DeclarativeBase):
    """Base class for all ORM models."""

    @declared_attr.directive
    def __tablename__(cls) -> str:  # noqa: N805
        """Derive snake_case table names from the class name."""
        return re.sub(r"(?<!^)(?=[A-Z])", "_", cls.__name__).lower()


class TimestampMixin:
    """Adds server-side created/updated timestamps."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

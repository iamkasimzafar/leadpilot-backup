"""Declarative base and shared column mixins.

Import every model module here so Alembic's autogenerate sees the full
metadata. Models are imported for their side effect of registering on Base.
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


# --- Model registry (extend as you add models) --------------------------------
# Imported for the side effect of registering on Base.metadata, so Alembic
# autogenerate can see them. Placed last to avoid circular imports.
# from app.models.user import User

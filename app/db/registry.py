"""Model registry for Alembic autogenerate.

Importing a model class registers it on Base.metadata. Alembic compares that
metadata against the live database, so every model must be imported somewhere
before autogenerate runs -- this module is that place.

It is deliberately separate from app/db/base.py: models import Base from there,
so importing models *into* base.py would create a circular import.

Extend the imports below as you add models.
"""

from app.db.base import Base
from app.models.user import EmailVerificationToken, PasswordResetToken, User

__all__ = ["Base", "EmailVerificationToken", "PasswordResetToken", "User"]

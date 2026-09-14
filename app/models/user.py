"""User and password-reset ORM models."""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin


def _uuid_str() -> str:
    return str(uuid.uuid4())


class User(Base, TimestampMixin):
    """An account that can sign in to LeadPilot."""

    # CHAR(36) rather than a native UUID type: portable across MySQL and the
    # SQLite used by the test suite.
    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=_uuid_str
    )
    email: Mapped[str] = mapped_column(
        String(255), unique=True, index=True, nullable=False
    )
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    full_name: Mapped[str | None] = mapped_column(String(255), nullable=True)

    is_active: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=func.true(), nullable=False
    )
    is_superuser: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=func.false(), nullable=False
    )

    # Email ownership is confirmed via a link; sign-in is still allowed while
    # unverified so a failed delivery cannot lock someone out of their account.
    is_verified: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=func.false(), nullable=False
    )
    verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    last_login_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    reset_tokens: Mapped[list["PasswordResetToken"]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
        lazy="selectin",
    )
    verification_tokens: Mapped[list["EmailVerificationToken"]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
        lazy="selectin",
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<User {self.email}>"


class PasswordResetToken(Base):
    """A single-use, expiring token for the forgot-password flow.

    Only the SHA-256 hash of the token is stored, so a database leak does not
    hand out working reset links.
    """

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=_uuid_str
    )
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("user.id", ondelete="CASCADE"), index=True, nullable=False
    )
    token_hash: Mapped[str] = mapped_column(
        String(64), unique=True, index=True, nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    user: Mapped[User] = relationship(back_populates="reset_tokens")

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<PasswordResetToken user_id={self.user_id}>"


class EmailVerificationToken(Base):
    """A single-use, expiring token proving the user controls their address.

    Stored as a SHA-256 hash for the same reason as PasswordResetToken.
    """

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid_str)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("user.id", ondelete="CASCADE"), index=True, nullable=False
    )
    token_hash: Mapped[str] = mapped_column(
        String(64), unique=True, index=True, nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    user: Mapped[User] = relationship(back_populates="verification_tokens")

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<EmailVerificationToken user_id={self.user_id}>"

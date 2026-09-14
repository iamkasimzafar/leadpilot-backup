"""Data access for users, password-reset and email-verification tokens.

The two token repositories are deliberately written out rather than sharing a
generic base: SQLAlchemy's typed columns do not survive a `BaseRepository[T]`
abstraction cleanly, and the duplication is three lines each.
"""

from datetime import UTC, datetime

from sqlalchemy import select

from app.models.user import EmailVerificationToken, PasswordResetToken, User
from app.repositories.base import BaseRepository


class UserRepository(BaseRepository[User]):
    model = User

    async def get_by_email(self, email: str) -> User | None:
        """Look up a user by email. Emails are stored and compared lowercased."""
        return await self.find_one_by(email=email.strip().lower())


class PasswordResetTokenRepository(BaseRepository[PasswordResetToken]):
    model = PasswordResetToken

    async def get_usable(self, token_hash: str) -> PasswordResetToken | None:
        """Return the token only if it exists, is unused, and has not expired."""
        stmt = select(PasswordResetToken).where(
            PasswordResetToken.token_hash == token_hash,
            PasswordResetToken.used_at.is_(None),
            PasswordResetToken.expires_at > datetime.now(UTC),
        )
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def invalidate_for_user(self, user_id: str) -> None:
        """Mark every outstanding token for a user as used, so a second link
        cannot be replayed once one has been redeemed."""
        stmt = select(PasswordResetToken).where(
            PasswordResetToken.user_id == user_id,
            PasswordResetToken.used_at.is_(None),
        )
        result = await self.db.execute(stmt)
        now = datetime.now(UTC)
        for token in result.scalars().all():
            token.used_at = now
        await self.db.flush()


class EmailVerificationTokenRepository(BaseRepository[EmailVerificationToken]):
    model = EmailVerificationToken

    async def get_usable(self, token_hash: str) -> EmailVerificationToken | None:
        """Return the token only if it exists, is unused, and has not expired."""
        stmt = select(EmailVerificationToken).where(
            EmailVerificationToken.token_hash == token_hash,
            EmailVerificationToken.used_at.is_(None),
            EmailVerificationToken.expires_at > datetime.now(UTC),
        )
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def invalidate_for_user(self, user_id: str) -> None:
        """Supersede every outstanding verification link for a user."""
        stmt = select(EmailVerificationToken).where(
            EmailVerificationToken.user_id == user_id,
            EmailVerificationToken.used_at.is_(None),
        )
        result = await self.db.execute(stmt)
        now = datetime.now(UTC)
        for token in result.scalars().all():
            token.used_at = now
        await self.db.flush()

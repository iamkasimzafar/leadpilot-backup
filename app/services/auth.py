"""Authentication business rules.

Owns the transaction boundary: routes call these methods and return the result.
Mail is not sent from here -- these methods return the plaintext token and the
route hands it to a background task, so a slow SMTP server never blocks (or
changes the timing of) the response.
"""

import hashlib
import secrets
from datetime import UTC, datetime, timedelta

from jwt.exceptions import InvalidTokenError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import google_identity
from app.core.exceptions import (
    ConflictError,
    EmailNotVerifiedError,
    UnauthorizedError,
    ValidationError,
)
from app.core.logging import get_logger
from app.core.security import (
    create_token,
    decode_token,
    hash_password,
    verify_password,
)
from app.models.user import EmailVerificationToken, PasswordResetToken, User
from app.repositories.user import (
    EmailVerificationTokenRepository,
    PasswordResetTokenRepository,
    UserRepository,
)
from app.schemas.auth import TokenPair
from app.services.base import BaseService

log = get_logger(__name__)

# How long a forgot-password link stays valid.
RESET_TOKEN_TTL = timedelta(hours=1)

# Verification links are longer-lived: people confirm email at their leisure.
VERIFICATION_TOKEN_TTL = timedelta(hours=48)


def _hash_token(token: str) -> str:
    """Hash a single-use token for storage. SHA-256 is right here: the token is
    already high-entropy, so this only needs to be one-way, not slow."""
    return hashlib.sha256(token.encode()).hexdigest()


class AuthService(BaseService):
    def __init__(self, db: AsyncSession) -> None:
        super().__init__(db)
        self.users = UserRepository(db)
        self.reset_tokens = PasswordResetTokenRepository(db)
        self.verification_tokens = EmailVerificationTokenRepository(db)

    # --- Token helpers ----------------------------------------------------
    def _issue_tokens(self, user: User) -> TokenPair:
        return TokenPair(
            access_token=create_token(user.id, token_type="access"),
            refresh_token=create_token(user.id, token_type="refresh"),
        )

    async def _new_verification_token(self, user: User) -> str:
        """Create a verification token, returning the plaintext to be mailed."""
        raw_token = secrets.token_urlsafe(32)
        await self.verification_tokens.create(
            user_id=user.id,
            token_hash=_hash_token(raw_token),
            expires_at=datetime.now(UTC) + VERIFICATION_TOKEN_TTL,
        )
        return raw_token

    # --- Registration -----------------------------------------------------
    async def register(
        self, email: str, password: str, full_name: str | None = None
    ) -> tuple[User, str]:
        """Create an account.

        Returns the user and the plaintext verification token so the route can
        email it. No session is issued: the address must be confirmed before
        the account can sign in.
        """
        normalised = email.strip().lower()

        if await self.users.get_by_email(normalised) is not None:
            raise ConflictError("An account with that email already exists.")

        user = await self.users.create(
            email=normalised,
            hashed_password=hash_password(password),
            full_name=full_name.strip() if full_name else None,
        )
        verification_token = await self._new_verification_token(user)
        await self.commit()

        log.info("auth.registered", user_id=user.id)
        return user, verification_token

    # --- Login ------------------------------------------------------------
    async def authenticate(self, email: str, password: str) -> TokenPair:
        user = await self.users.get_by_email(email)

        # Always run a hash comparison so a missing account and a wrong
        # password take the same time, leaking nothing about which it was.
        stored_hash = user.hashed_password if user else hash_password("dummy-password")
        password_ok = verify_password(password, stored_hash)

        if user is None or not password_ok:
            raise UnauthorizedError("Incorrect email or password.")

        if not user.is_active:
            raise UnauthorizedError("This account has been deactivated.")

        # Email ownership must be confirmed before the account can be used.
        # Raised after the password check so this never reveals whether an
        # address is registered to someone who does not know the password.
        if not user.is_verified:
            raise EmailNotVerifiedError()

        user.last_login_at = datetime.now(UTC)
        tokens = self._issue_tokens(user)
        await self.commit()

        log.info("auth.login", user_id=user.id)
        return tokens

    # --- Google sign-in ---------------------------------------------------
    async def authenticate_google(
        self, access_token: str
    ) -> tuple[TokenPair, User, bool]:
        """Sign in -- or sign up -- with a Google access token.

        Returns the tokens, the user, and whether the account was created just
        now, so the route can send the welcome email exactly once.
        """
        identity = await google_identity.verify_access_token(access_token)

        now = datetime.now(UTC)

        # One account per email, however it was first created. Someone who
        # registered with a password and later clicks "Continue with Google"
        # lands in that same account -- Google becomes a second way in, never a
        # second account.
        user = await self.users.get_by_email(identity.email)
        created = False

        if user is None:
            try:
                # Savepoint: if a concurrent request creates this address first,
                # the unique index on `email` rejects our insert and only this
                # nested block rolls back.
                async with self.db.begin_nested():
                    user = await self.users.create(
                        email=identity.email,
                        # Nobody knows this value, so the account has no usable
                        # password until its owner sets one through "forgot
                        # password".
                        hashed_password=hash_password(secrets.token_urlsafe(32)),
                        full_name=identity.full_name,
                        is_verified=True,
                        verified_at=now,
                    )
                created = True
            except IntegrityError:
                # Lost the race: the account exists now, so link to it.
                user = await self.users.get_by_email(identity.email)
                if user is None:
                    raise

        if not created:
            if not user.is_active:
                raise UnauthorizedError("This account has been deactivated.")

            if not user.is_verified:
                # Google has just proved who owns this address, which our own
                # email link never did. Whoever registered it may not be that
                # person: someone can sign up with a victim's address and wait.
                # Verifying the account while keeping *their* password would
                # hand them a working login, so the password goes too.
                user.hashed_password = hash_password(secrets.token_urlsafe(32))
                user.is_verified = True
                user.verified_at = now

            if not user.full_name and identity.full_name:
                user.full_name = identity.full_name

        user.last_login_at = now
        tokens = self._issue_tokens(user)
        await self.commit()

        log.info("auth.login.google", user_id=user.id, created=created)
        return tokens, user, created

    # --- Refresh ----------------------------------------------------------
    async def refresh(self, refresh_token: str) -> TokenPair:
        try:
            payload = decode_token(refresh_token, expected_type="refresh")
        except InvalidTokenError as exc:
            raise UnauthorizedError("Invalid or expired refresh token.") from exc

        user_id = payload.get("sub")
        if not user_id:
            raise UnauthorizedError("Malformed refresh token.")

        user = await self.users.get(user_id)
        if user is None or not user.is_active:
            raise UnauthorizedError("User no longer active.")

        return self._issue_tokens(user)

    # --- Email verification ----------------------------------------------
    async def request_verification(self, email: str) -> tuple[User, str] | None:
        """Issue a fresh verification token for an unverified account.

        Returns None when there is nothing to send (unknown address, inactive,
        or already verified); the route responds identically either way.
        """
        user = await self.users.get_by_email(email)
        if user is None or not user.is_active or user.is_verified:
            log.info("auth.verification_requested_noop")
            return None

        # Supersede any outstanding link so only the newest one works.
        await self.verification_tokens.invalidate_for_user(user.id)
        raw_token = await self._new_verification_token(user)
        await self.commit()

        log.info("auth.verification_requested", user_id=user.id)
        return user, raw_token

    async def verify_email(self, token: str) -> tuple[User, bool]:
        """Confirm an address.

        Returns the user and whether this call is what flipped them to
        verified, so the route only sends the welcome mail once even if the
        link is opened twice (mail scanners routinely prefetch links).
        """
        record: EmailVerificationToken | None = (
            await self.verification_tokens.get_usable(_hash_token(token))
        )
        if record is None:
            raise ValidationError("This verification link is invalid or has expired.")

        user = await self.users.get(record.user_id)
        if user is None or not user.is_active:
            raise ValidationError("This verification link is no longer usable.")

        newly_verified = not user.is_verified
        if newly_verified:
            user.is_verified = True
            user.verified_at = datetime.now(UTC)

        # Burn every outstanding token, including this one.
        await self.verification_tokens.invalidate_for_user(user.id)
        await self.commit()

        log.info("auth.email_verified", user_id=user.id, first_time=newly_verified)
        return user, newly_verified

    # --- Forgot password --------------------------------------------------
    async def request_password_reset(self, email: str) -> str | None:
        """Create a reset token for the address, if an account exists.

        Returns the plaintext token so the caller can mail it. The route always
        responds the same way regardless, so this never reveals whether the
        address is registered.
        """
        user = await self.users.get_by_email(email)
        if user is None or not user.is_active:
            log.info("auth.reset_requested_unknown_email")
            return None

        raw_token = secrets.token_urlsafe(32)
        await self.reset_tokens.create(
            user_id=user.id,
            token_hash=_hash_token(raw_token),
            expires_at=datetime.now(UTC) + RESET_TOKEN_TTL,
        )
        await self.commit()

        log.info("auth.reset_requested", user_id=user.id)
        return raw_token

    # --- Reset password ---------------------------------------------------
    async def reset_password(self, token: str, new_password: str) -> None:
        record: PasswordResetToken | None = await self.reset_tokens.get_usable(
            _hash_token(token)
        )
        if record is None:
            raise ValidationError("This reset link is invalid or has expired.")

        user = await self.users.get(record.user_id)
        if user is None or not user.is_active:
            raise ValidationError("This reset link is no longer usable.")

        user.hashed_password = hash_password(new_password)
        # Burn every outstanding token, including this one.
        await self.reset_tokens.invalidate_for_user(user.id)
        await self.commit()

        log.info("auth.password_reset", user_id=user.id)


def reset_token_ttl_hours() -> int:
    return int(RESET_TOKEN_TTL.total_seconds() // 3600)


def verification_token_ttl_hours() -> int:
    return int(VERIFICATION_TOKEN_TTL.total_seconds() // 3600)


__all__ = ["AuthService", "reset_token_ttl_hours", "verification_token_ttl_hours"]

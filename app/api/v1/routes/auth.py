"""Authentication endpoints."""

from fastapi import APIRouter, BackgroundTasks, status

from app.api.deps import CurrentUser, DbSession
from app.core import email_templates
from app.core.email import send_email
from app.schemas.auth import (
    ForgotPasswordRequest,
    LoginRequest,
    RefreshRequest,
    RegisterRequest,
    ResendVerificationRequest,
    ResetPasswordRequest,
    TokenPair,
    VerifyEmailRequest,
)
from app.schemas.common import Message
from app.schemas.user import UserRead
from app.services.auth import (
    AuthService,
    reset_token_ttl_hours,
    verification_token_ttl_hours,
)

router = APIRouter()

# Identical whether or not the address exists, so neither endpoint can be used
# to enumerate accounts.
_GENERIC_RESET_REPLY = "If that email is registered, a reset link is on its way."
_GENERIC_VERIFY_REPLY = "If that address needs confirming, a new link is on its way."


def _queue_password_reset(tasks: BackgroundTasks, to: str, token: str) -> None:
    subject, html, text = email_templates.password_reset(
        token, ttl_hours=reset_token_ttl_hours()
    )
    tasks.add_task(send_email, to=to, subject=subject, html=html, text=text)


def _queue_verification(tasks: BackgroundTasks, to: str, token: str) -> None:
    subject, html, text = email_templates.email_verification(
        token, ttl_hours=verification_token_ttl_hours()
    )
    tasks.add_task(send_email, to=to, subject=subject, html=html, text=text)


def _queue_welcome(tasks: BackgroundTasks, to: str, full_name: str | None) -> None:
    subject, html, text = email_templates.welcome(full_name)
    tasks.add_task(send_email, to=to, subject=subject, html=html, text=text)


@router.post(
    "/register", response_model=Message, status_code=status.HTTP_201_CREATED
)
async def register(
    payload: RegisterRequest, db: DbSession, background_tasks: BackgroundTasks
) -> Message:
    """Create an account and email a verification link.

    No session is returned: the address must be confirmed before sign-in. The
    welcome email follows once verification succeeds.
    """
    user, verification_token = await AuthService(db).register(
        email=payload.email,
        password=payload.password,
        full_name=payload.full_name,
    )
    _queue_verification(background_tasks, user.email, verification_token)

    return Message(
        message="Account created. Check your email to confirm your address."
    )


@router.post("/login", response_model=TokenPair)
async def login(payload: LoginRequest, db: DbSession) -> TokenPair:
    """Exchange credentials for an access/refresh token pair."""
    return await AuthService(db).authenticate(
        email=payload.email, password=payload.password
    )


@router.post("/refresh", response_model=TokenPair)
async def refresh(payload: RefreshRequest, db: DbSession) -> TokenPair:
    """Exchange a valid refresh token for a new pair."""
    return await AuthService(db).refresh(payload.refresh_token)


@router.post("/verify-email", response_model=UserRead)
async def verify_email(
    payload: VerifyEmailRequest, db: DbSession, background_tasks: BackgroundTasks
) -> UserRead:
    """Confirm an email address using the token from the emailed link.

    The welcome email is sent here rather than at registration, so it only
    reaches addresses that are known to work.
    """
    user, newly_verified = await AuthService(db).verify_email(payload.token)

    if newly_verified:
        _queue_welcome(background_tasks, user.email, user.full_name)

    return UserRead.model_validate(user)


@router.post("/resend-verification", response_model=Message)
async def resend_verification(
    payload: ResendVerificationRequest,
    db: DbSession,
    background_tasks: BackgroundTasks,
) -> Message:
    """Send a fresh verification link, if the address still needs confirming."""
    result = await AuthService(db).request_verification(payload.email)

    if result is not None:
        user, token = result
        _queue_verification(background_tasks, user.email, token)

    return Message(message=_GENERIC_VERIFY_REPLY)


@router.post("/forgot-password", response_model=Message)
async def forgot_password(
    payload: ForgotPasswordRequest, db: DbSession, background_tasks: BackgroundTasks
) -> Message:
    """Start the password-reset flow.

    Always returns the same response so the endpoint cannot be used to discover
    which email addresses have accounts.
    """
    token = await AuthService(db).request_password_reset(payload.email)

    if token is not None:
        _queue_password_reset(background_tasks, payload.email, token)

    return Message(message=_GENERIC_RESET_REPLY)


@router.post("/reset-password", response_model=Message)
async def reset_password(payload: ResetPasswordRequest, db: DbSession) -> Message:
    """Complete the password-reset flow using a token from the email link."""
    await AuthService(db).reset_password(payload.token, payload.password)
    return Message(message="Your password has been updated.")


@router.get("/me", response_model=UserRead)
async def read_me(current_user: CurrentUser) -> UserRead:
    """Return the authenticated user. Proves the bearer flow end to end."""
    return UserRead.model_validate(current_user)

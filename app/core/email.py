"""SMTP delivery.

`send_email` is the single way mail leaves the application. With
settings.EMAIL_ENABLED false it logs the message instead of sending, so the
password-reset and verification flows can be exercised locally with no
credentials -- the link is printed to the console.

Sending is blocking (smtplib), so it is pushed to a worker thread; callers are
expected to invoke it from a background task rather than inline in a request.
"""

import smtplib
from email.message import EmailMessage

import anyio.to_thread

from app.core.config import settings
from app.core.logging import get_logger

log = get_logger(__name__)


def _build_message(to: str, subject: str, html: str, text: str) -> EmailMessage:
    message = EmailMessage()
    message["From"] = settings.email_from
    message["To"] = to
    message["Subject"] = subject

    # Plain text first, then HTML: mail clients render the last part they can.
    message.set_content(text)
    message.add_alternative(html, subtype="html")

    return message


def _send_sync(message: EmailMessage) -> None:
    """Blocking SMTP send. Runs in a worker thread via send_email."""
    with smtplib.SMTP(
        settings.SMTP_HOST, settings.SMTP_PORT, timeout=settings.SMTP_TIMEOUT
    ) as smtp:
        if settings.SMTP_STARTTLS:
            smtp.starttls()
        if settings.SMTP_USER:
            smtp.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
        smtp.send_message(message)


async def send_email(*, to: str, subject: str, html: str, text: str) -> bool:
    """Deliver one message. Returns True if it was handed to the SMTP server.

    Never raises: a mail failure must not break the request that triggered it,
    and for the auth flows it must not reveal whether an address is registered.
    """
    if not settings.EMAIL_ENABLED:
        # Dev mode: log the body so reset/verify links are usable from console.
        log.info("email.suppressed", to=to, subject=subject, body=text)
        return False

    try:
        message = _build_message(to, subject, html, text)
        await anyio.to_thread.run_sync(_send_sync, message)
        log.info("email.sent", to=to, subject=subject)
        return True
    except Exception as exc:  # Deliberately swallowed -- see the docstring.
        log.error("email.failed", to=to, subject=subject, error=str(exc))
        return False

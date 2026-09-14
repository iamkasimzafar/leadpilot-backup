"""Email bodies.

Kept as plain functions returning (subject, html, text) so they stay trivially
testable and carry no template-engine dependency. Every mail ships both an HTML
and a plain-text part.
"""

from urllib.parse import quote

from app.core.config import settings

_BRAND = "#2563EB"


def _shell(heading: str, body: str, button_label: str, button_url: str) -> str:
    """Single-column responsive layout. Inline styles only -- mail clients
    strip <style> blocks."""
    return f"""\
<!doctype html>
<html>
  <body style="margin:0;padding:24px;background:#f1f5f9;
               font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
      <tr><td align="center">
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
               style="max-width:480px;background:#ffffff;border:1px solid #e2e8f0;
                      border-radius:12px;padding:32px;">
          <tr><td style="font-size:20px;font-weight:700;color:#0f172a;
                         padding-bottom:8px;">LeadPilot</td></tr>
          <tr><td style="font-size:18px;font-weight:600;color:#0f172a;
                         padding-bottom:12px;">{heading}</td></tr>
          <tr><td style="font-size:14px;line-height:1.6;color:#475569;
                         padding-bottom:24px;">{body}</td></tr>
          <tr><td>
            <a href="{button_url}"
               style="display:inline-block;background:{_BRAND};color:#ffffff;
                      text-decoration:none;font-size:14px;font-weight:600;
                      padding:12px 20px;border-radius:8px;">{button_label}</a>
          </td></tr>
          <tr><td style="font-size:12px;line-height:1.6;color:#94a3b8;
                         padding-top:24px;word-break:break-all;">
            If the button does not work, paste this link into your browser:<br>
            {button_url}
          </td></tr>
        </table>
      </td></tr>
    </table>
  </body>
</html>"""


def password_reset(token: str, *, ttl_hours: int = 1) -> tuple[str, str, str]:
    url = f"{settings.FRONTEND_URL.rstrip('/')}/reset-password?token={quote(token)}"
    subject = "Reset your LeadPilot password"

    html = _shell(
        "Reset your password",
        "We received a request to set a new password for your LeadPilot account. "
        f"This link expires in {ttl_hours} hour(s). "
        "If you did not ask for this, you can safely ignore this email.",
        "Set a new password",
        url,
    )
    text = (
        "Reset your LeadPilot password\n\n"
        f"Open this link to choose a new password (expires in {ttl_hours} hour(s)):\n"
        f"{url}\n\n"
        "If you did not request this, ignore this email."
    )
    return subject, html, text


def welcome(full_name: str | None = None) -> tuple[str, str, str]:
    """Sent alongside the verification mail when an account is created."""
    url = f"{settings.FRONTEND_URL.rstrip('/')}/"
    greeting = f"Hi {full_name.split()[0]}," if full_name else "Hi,"
    subject = "Welcome to LeadPilot"

    html = _shell(
        "Welcome to LeadPilot",
        f"{greeting} your account is ready. LeadPilot finds buyers, keeps your "
        "pipeline moving, and follows up for you &mdash; so your team can focus "
        "on the conversations that matter."
        "<br><br>Start by running a Lead Radar task to discover your first leads.",
        "Open LeadPilot",
        url,
    )
    text = (
        "Welcome to LeadPilot\n\n"
        f"{greeting} your account is ready. LeadPilot finds buyers, keeps your "
        "pipeline moving, and follows up for you.\n\n"
        "Start by running a Lead Radar task to discover your first leads.\n\n"
        f"{url}"
    )
    return subject, html, text


def email_verification(token: str, *, ttl_hours: int = 48) -> tuple[str, str, str]:
    url = f"{settings.FRONTEND_URL.rstrip('/')}/verify-email?token={quote(token)}"
    subject = "Confirm your LeadPilot email address"

    html = _shell(
        "Confirm your email",
        "Welcome to LeadPilot. Confirm this address to finish setting up your "
        f"account. This link expires in {ttl_hours} hour(s).",
        "Confirm email address",
        url,
    )
    text = (
        "Confirm your LeadPilot email address\n\n"
        f"Open this link to confirm (expires in {ttl_hours} hour(s)):\n"
        f"{url}\n\n"
        "If you did not create a LeadPilot account, ignore this email."
    )
    return subject, html, text

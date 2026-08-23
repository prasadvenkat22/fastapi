"""Outbound email over SMTP, and the rules that keep it from breaking requests.

Nothing in this codebase could send mail before this, which is why the only
recovery path for a forgotten password was an admin issuing a temporary one
by hand. Written against plain SMTP rather than a provider SDK so Gmail,
SendGrid, Mailgun, SES and Postmark are all a change of environment variables
rather than a change of code — and so it adds no dependency, since smtplib is
in the standard library.

Three rules, and the first two matter more than delivery does.

SENDING NEVER BREAKS THE CALLER. Every failure is caught and logged. A
password that was successfully changed must not report failure because a mail
server was briefly unreachable — the change already happened, and telling the
user it did not is worse than a missing notification.

UNCONFIGURED IS A SUPPORTED STATE, not an error. With no SMTP_HOST set,
send() logs what it would have sent and returns False. The app runs, the
endpoints work, and the absence is visible in the log rather than as a
stack trace on a request nobody could have anticipated.

AND IT NEVER LOGS THE BODY. Reset links are credentials for the ~30 minutes
they live. The log records the recipient and the subject, never the contents.
"""

import logging
import os
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr

logger = logging.getLogger(__name__)

SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
SMTP_STARTTLS = os.getenv("SMTP_STARTTLS", "true").lower() == "true"
SMTP_TIMEOUT = float(os.getenv("SMTP_TIMEOUT", "10"))

# Gmail rewrites the envelope sender to the authenticated account anyway, so
# this only controls the display name unless you have configured a verified
# alias. Defaults to the login so a misconfiguration is obvious in the header.
MAIL_FROM = os.getenv("MAIL_FROM", SMTP_USER)
MAIL_FROM_NAME = os.getenv("MAIL_FROM_NAME", "DataIQ Systems")

# Where a reset link points. No trailing slash; the path is appended.
APP_BASE_URL = os.getenv("APP_BASE_URL", "").rstrip("/")


def is_configured() -> bool:
    return bool(SMTP_HOST and SMTP_USER and SMTP_PASSWORD)


def send(to: str, subject: str, body: str) -> bool:
    """Send one plain-text message. True if it left this process.

    Returns rather than raises, because every caller is in a request path
    where the important work has already succeeded.
    """
    if not is_configured():
        logger.warning(
            "Email NOT sent to %s (%r): SMTP is not configured. Set SMTP_HOST, "
            "SMTP_USER and SMTP_PASSWORD to turn this on.", to, subject,
        )
        return False

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr((MAIL_FROM_NAME, MAIL_FROM))
    msg["To"] = to
    msg.set_content(body)

    try:
        if SMTP_PORT == 465:
            # Implicit TLS. Gmail offers both; 587 with STARTTLS is the usual one.
            with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=SMTP_TIMEOUT,
                                  context=ssl.create_default_context()) as s:
                s.login(SMTP_USER, SMTP_PASSWORD)
                s.send_message(msg)
        else:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=SMTP_TIMEOUT) as s:
                if SMTP_STARTTLS:
                    s.starttls(context=ssl.create_default_context())
                s.login(SMTP_USER, SMTP_PASSWORD)
                s.send_message(msg)
    except smtplib.SMTPAuthenticationError:
        # By far the most common failure with Gmail, and the fix is specific
        # enough to be worth its own branch.
        logger.error(
            "Email to %s failed: SMTP authentication rejected. With Gmail this "
            "means SMTP_PASSWORD is an account password rather than an App "
            "Password, or 2-Step Verification is off so App Passwords cannot "
            "be created.", to,
        )
        return False
    except Exception as exc:  # noqa: BLE001 — no mail failure may reach the caller
        logger.error("Email to %s (%r) failed: %s: %s", to, subject,
                     type(exc).__name__, exc)
        return False

    logger.info("Email sent to %s (%r).", to, subject)
    return True


# ---------------------------------------------------------------------------
# Messages. Kept here so wording is in one place rather than inline in routes.
# ---------------------------------------------------------------------------

def send_password_changed(to: str, when: str, by_admin: bool = False) -> bool:
    """Told after the fact, which is the point: this is how someone learns
    their account was taken over, so it goes out even when the change was
    expected."""
    how = ("An administrator reset the password on your account."
           if by_admin else "The password on your account was changed.")
    return send(
        to, "Your password was changed",
        f"{how}\n\nWhen: {when}\n\n"
        "If this was you, nothing further is needed.\n\n"
        "If it was NOT you, someone else has access to this account. Reset the "
        "password immediately and tell an administrator.\n",
    )


def send_account_created(to: str, role: str, has_password: bool) -> bool:
    reset_hint = (
        "A password has already been set for you — use the one you were given, "
        "and change it after signing in."
        if has_password else
        "No password has been set yet. Use 'forgot password' at "
        f"{APP_BASE_URL or '<the application URL>'}/auth/forgot-password to "
        "choose one, or ask an administrator."
    )
    return send(
        to, "An account has been created for you",
        f"An account has been created for you with the role: {role}.\n\n"
        f"Sign in with this email address.\n\n{reset_hint}\n",
    )


def send_password_reset(to: str, token: str, minutes: int) -> bool:
    """The one message that carries a credential. Never logged."""
    if APP_BASE_URL:
        action = f"Open this link to choose a new password:\n\n{APP_BASE_URL}/auth/reset-password?token={token}\n"
    else:
        # Without a base URL a link cannot be built, so give the raw token and
        # say what to do with it rather than sending a broken link.
        action = ("POST this token to /auth/reset-password together with your "
                  f"new password:\n\n{token}\n")
    return send(
        to, "Reset your password",
        f"Someone asked to reset the password for this account.\n\n{action}\n"
        f"The link expires in {minutes} minutes and works once.\n\n"
        "If you did not ask for this, ignore this message — your password has "
        "not changed and nobody can use this link without it.\n",
    )

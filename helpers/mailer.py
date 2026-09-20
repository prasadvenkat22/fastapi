"""Outbound email, and the rules that keep it from breaking requests.

TWO TRANSPORTS. SendGrid's HTTP API is tried first when SENDGRID_API_KEY is
set; SMTP is the fallback. That order is not a preference, it is a necessity on
this host:

    ufw:  default allow (outgoing)
    host -> smtp.gmail.com:587   TIMEOUT
    host -> smtp.gmail.com:465   TIMEOUT
    host -> api.sendgrid.com:443 OPEN

The provider blocks SMTP egress, which DigitalOcean does by default, so every
message this module has ever tried to send from production has failed --
silently, by design, because send() returns False rather than raising. That
included forgot-password and reset-password. An HTTP API on 443 is the only
path off this box (section 133).

SENDGRID NEEDS A VERIFIED SENDER. MAIL_FROM must be a verified Single Sender or
sit on an authenticated domain, or the API returns 403 and nothing arrives. The
error is logged with SendGrid's own message, which names the problem exactly.

Nothing in this codebase could send mail before this module, which is why the
only recovery path for a forgotten password was an admin issuing a temporary
one by hand. The SMTP path is still written against plain smtplib rather than a
provider SDK, so Gmail, Mailgun, SES and Postmark remain a change of
environment variables; the SendGrid path uses httpx, which the project already
depends on, so neither adds a dependency.

Three rules, and the first two matter more than delivery does.

SENDING NEVER BREAKS THE CALLER. Every failure is caught and logged. A
password that was successfully changed must not report failure because a mail
server was briefly unreachable — the change already happened, and telling the
user it did not is worse than a missing notification.

UNCONFIGURED IS A SUPPORTED STATE, not an error. With neither transport set,
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

# HTTP transport, tried first. Port 443, so it survives an SMTP block.
SENDGRID_API_KEY = os.getenv("SENDGRID_API_KEY", "")
SENDGRID_URL = os.getenv("SENDGRID_URL", "https://api.sendgrid.com/v3/mail/send")
SENDGRID_TIMEOUT = float(os.getenv("SENDGRID_TIMEOUT", "10"))

SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
SMTP_STARTTLS = os.getenv("SMTP_STARTTLS", "true").lower() == "true"
SMTP_TIMEOUT = float(os.getenv("SMTP_TIMEOUT", "10"))

# Gmail rewrites the envelope sender to the authenticated account anyway, so
# this only controls the display name unless you have configured a verified
# alias. Defaults to the login so a misconfiguration is obvious in the header.
#
# With SendGrid the address must sit on the authenticated domain. dataaisys.com
# is authenticated (the three SendGrid CNAMEs, 2026-09-20), so any mailbox on
# it works as a sender: services@ for account mail, trading@ for alerts.
MAIL_FROM = os.getenv("MAIL_FROM", SMTP_USER)
MAIL_FROM_NAME = os.getenv("MAIL_FROM_NAME", "Data AI Systems")

# Trading alerts come from their own address so they can be filtered, and so
# a price alert never looks like a password-reset mail. Both fall back to the
# account sender, so an env without them still delivers.
ALERT_MAIL_FROM = os.getenv("ALERT_MAIL_FROM", "") or MAIL_FROM
ALERT_MAIL_FROM_NAME = os.getenv("ALERT_MAIL_FROM_NAME", "Data AI Systems Trading")

# Where a visitor's inquiry (the public contact form) is delivered. Empty means
# the inquiry is stored but nobody is told, which the log says out loud.
CONTACT_EMAIL = os.getenv("CONTACT_EMAIL", "")

# Where a reset link points. No trailing slash; the path is appended.
APP_BASE_URL = os.getenv("APP_BASE_URL", "").rstrip("/")


def is_configured() -> bool:
    """Either transport counts."""
    return bool(SENDGRID_API_KEY) or bool(SMTP_HOST and SMTP_USER and SMTP_PASSWORD)


def _send_sendgrid(to: str, subject: str, body: str, sender: str,
                   sender_name: str, reply_to: str = "") -> bool:
    """POST one message to SendGrid. 202 Accepted is the success code.

    Never raises and never logs `body` -- a reset link is a credential for the
    thirty minutes it lives, and the same rule applies whichever transport
    carries it.
    """
    payload = {
        "personalizations": [{"to": [{"email": to}]}],
        "from": {"email": sender, "name": sender_name},
        "subject": subject,
        "content": [{"type": "text/plain", "value": body}],
    }
    if reply_to:
        payload["reply_to"] = {"email": reply_to}
    try:
        import httpx

        r = httpx.post(
            SENDGRID_URL,
            headers={"Authorization": f"Bearer {SENDGRID_API_KEY}",
                     "Content-Type": "application/json"},
            json=payload,
            timeout=SENDGRID_TIMEOUT,
        )
    except Exception as exc:  # noqa: BLE001 — no mail failure may reach the caller
        logger.error("Email to %s (%r) failed: %s: %s", to, subject,
                     type(exc).__name__, exc)
        return False

    if r.status_code == 202:
        logger.info("Email sent to %s (%r) via SendGrid.", to, subject)
        return True
    if r.status_code in (401, 403):
        # The two failures worth naming, because the fix differs and neither
        # is visible from a generic status code.
        logger.error(
            "Email to %s rejected by SendGrid (%d). 401 means SENDGRID_API_KEY "
            "is wrong; 403 almost always means the sender (%s) is not a "
            "verified Single Sender and its domain is not authenticated. "
            "SendGrid said: %s", to, r.status_code, sender, r.text[:300],
        )
        return False
    logger.error("Email to %s (%r) failed: SendGrid returned %d: %s",
                 to, subject, r.status_code, r.text[:300])
    return False


def send(to: str, subject: str, body: str, *, sender: str = "",
         sender_name: str = "", reply_to: str = "") -> bool:
    """Send one plain-text message. True if it left this process.

    Returns rather than raises, because every caller is in a request path
    where the important work has already succeeded.

    `sender` overrides MAIL_FROM for the one message; send_alert() uses it so
    trading mail carries its own address. `reply_to` lets a forwarded inquiry
    be answered by hitting reply, without the notification pretending to be
    FROM the visitor (which SendGrid would refuse anyway).
    """
    sender = sender or MAIL_FROM
    sender_name = sender_name or MAIL_FROM_NAME
    if not is_configured():
        logger.warning(
            "Email NOT sent to %s (%r): no transport configured. Set "
            "SENDGRID_API_KEY (works on this host), or SMTP_HOST, SMTP_USER "
            "and SMTP_PASSWORD (blocked by the provider here).", to, subject,
        )
        return False

    if SENDGRID_API_KEY:
        return _send_sendgrid(to, subject, body, sender, sender_name, reply_to)

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr((sender_name, sender))
    msg["To"] = to
    if reply_to:
        msg["Reply-To"] = reply_to
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
    except OSError as exc:
        # Errno 101 on this host: the provider blocks SMTP egress. Named
        # because the generic branch below reads as a transient network blip
        # and this one never recovers.
        logger.error(
            "Email to %s (%r) failed at the socket: %s. On this droplet ports "
            "587 and 465 are blocked by the provider -- set SENDGRID_API_KEY "
            "to send over 443 instead.", to, subject, exc,
        )
        return False
    except Exception as exc:  # noqa: BLE001 — no mail failure may reach the caller
        logger.error("Email to %s (%r) failed: %s: %s", to, subject,
                     type(exc).__name__, exc)
        return False

    logger.info("Email sent to %s (%r) via SMTP.", to, subject)
    return True


def send_alert(to: str, subject: str, body: str) -> bool:
    """A trading alert: same transport, the trading sender."""
    return send(to, subject, body, sender=ALERT_MAIL_FROM,
                sender_name=ALERT_MAIL_FROM_NAME)


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


# ---------------------------------------------------------------------------
# The public contact form. Two messages per inquiry: the owner is told, with
# the visitor's address as Reply-To so answering is one click; the visitor
# gets an acknowledgement so the form is known to have worked.
# ---------------------------------------------------------------------------

def send_inquiry_notification(*, name: str, email: str, company: str,
                              phone: str, interest: str, message: str,
                              demo_date: str, registration_id: int) -> bool:
    """To the owner. Returns False, and says why in the log, when
    CONTACT_EMAIL is unset -- the row is still in the database."""
    if not CONTACT_EMAIL:
        logger.warning("Inquiry #%s from %s stored but NOT forwarded: "
                       "CONTACT_EMAIL is not set.", registration_id, email)
        return False
    lines = [
        f"New inquiry #{registration_id} from the website.",
        "",
        f"Name:      {name}",
        f"Email:     {email}",
        f"Company:   {company or '-'}",
        f"Phone:     {phone or '-'}",
        f"Interest:  {interest or '-'}",
        f"Demo date: {demo_date or 'not requested'}",
        "",
        "Message:",
        message or "(none)",
        "",
        "Reply to this mail to answer them directly.",
        f"It is also listed under Demo Registrations at {APP_BASE_URL or '<the application URL>'}/registrations.",
    ]
    subject = f"Inquiry from {name}" + (f" ({company})" if company else "")
    return send(CONTACT_EMAIL, subject, "\n".join(lines) + "\n", reply_to=email)


def send_inquiry_acknowledgement(to: str, name: str, demo_date: str) -> bool:
    """To the visitor. Nothing they typed is echoed back except the demo
    date, so the form cannot be used to send arbitrary text to a third party."""
    when = (f"You asked about a demo around {demo_date}; we will confirm a time.\n\n"
            if demo_date else "")
    return send(
        to, "We received your inquiry",
        f"Hello {name},\n\n"
        "Thank you for contacting Data AI Systems. Your inquiry has been "
        "received and a person will reply, usually within one business day.\n\n"
        f"{when}"
        "If you did not submit this form, ignore this message.\n\n"
        "Data AI Systems\n"
        f"{APP_BASE_URL or 'https://dataaisys.com'}\n",
    )


# ---------------------------------------------------------------------------
# The product-updates list (section 203). Double opt-in: nothing is sent to an
# address that has not clicked the confirmation link we mailed it.
# ---------------------------------------------------------------------------

def send_subscribe_confirm(to: str, name: str, confirm_url: str,
                           unsubscribe_url: str) -> bool:
    """The opt-in mail. Carries the token, so never logged."""
    hello = f"Hello {name}," if name else "Hello,"
    return send(
        to, "Confirm your Data AI Systems updates",
        f"{hello}\n\n"
        "Someone -- we hope you -- asked for occasional updates from Data AI "
        "Systems about the FinAI Options Auto-Trader and our data and AI "
        "work. To confirm, open this link:\n\n"
        f"{confirm_url}\n\n"
        "The link works for 7 days. If you did not ask for this, ignore this "
        "message and nothing will be sent.\n\n"
        f"Unsubscribe at any time: {unsubscribe_url}\n",
    )


def send_subscriber_confirmed(*, email: str, name: str, interest: str,
                              source: str, subscriber_id: int) -> bool:
    """To the owner, once, when an address confirms."""
    if not CONTACT_EMAIL:
        logger.warning("Subscriber #%s (%s) confirmed but CONTACT_EMAIL is "
                       "not set; nobody told.", subscriber_id, email)
        return False
    return send(
        CONTACT_EMAIL, f"New updates subscriber: {email}",
        f"Subscriber #{subscriber_id} confirmed.\n\n"
        f"Email:    {email}\nName:     {name or '-'}\n"
        f"Interest: {interest or '-'}\nFrom:     {source or '-'}\n\n"
        f"The list is at {APP_BASE_URL or '<the application URL>'}/subscribers.\n",
    )

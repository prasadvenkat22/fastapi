"""The public contact form: POST /api/contact/inquiry.

THE ONE WRITE ENDPOINT WITHOUT A BEARER. Until 2026-09-20 the site's "Book a
Free Demo" button led to /registrations, whose form posted to
/CRUD/registrations/ -- an admin-only route -- so every visitor who tried got a
401 and nothing was recorded. This route exists so an anonymous visitor can
leave an inquiry, and it is the only thing they can do: one row in the
`registrations` table (the same table the admin's Demo Registrations page
lists), one mail to the owner, one acknowledgement to the visitor.

WHAT KEEPS IT FROM BEING A SPAM CANNON. Four things, none of them clever:

    nginx     `location = /api/contact/inquiry` sits in the `login` zone,
              five requests a minute per IP (app/nginx/conf.d/dataaisys.conf).
    honeypot  a `website` field the form renders hidden. Filled in means a
              bot; the request is answered 202 and nothing else happens, so
              the bot learns nothing.
    lengths   every field is capped; the message at 2000 characters.
    echo      the acknowledgement to the visitor repeats nothing they typed
              except the demo date. A form that echoed the message would be
              a way to send arbitrary text to any address.

The notification to the owner carries the visitor's address as Reply-To, so
answering is one click, and the mail is still FROM the verified sender --
SendGrid refuses anything else.

Sending never blocks the response: both mails run as background tasks and
mailer.send() returns False rather than raising, so a SendGrid outage costs a
notification, not the inquiry. The row is committed before either is queued.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, Request, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, ConfigDict, EmailStr, Field

import models_pgdb.models as models
from helpers import mailer
from helpers.auth_deps import require_admin
from routes.db_pgrs_router import db_dependency

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/contact", tags=["Contact"])


class InquiryRequest(BaseModel):
    first_name: str = Field(min_length=1, max_length=80)
    last_name: str = Field(default="", max_length=80)
    email: EmailStr
    company: str = Field(default="", max_length=120)
    phone: str = Field(default="", max_length=40)
    interest: str = Field(default="", max_length=120)
    message: str = Field(default="", max_length=2000)
    demo_date: Optional[datetime] = None
    # Honeypot. The form hides it; humans leave it empty.
    website: str = Field(default="", max_length=200)


class InquiryResponse(BaseModel):
    status: str
    id: Optional[int] = None


def _client_ip(request: Request) -> str:
    """nginx sets X-Forwarded-For (proxy_params.inc); fall back to the socket."""
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "?"


@router.post("/inquiry", response_model=InquiryResponse,
             status_code=status.HTTP_202_ACCEPTED)
async def submit_inquiry(body: InquiryRequest, request: Request,
                         db: db_dependency, background: BackgroundTasks):
    if body.website.strip():
        # A bot filled the hidden field. Same answer as success, no side effects.
        logger.info("Inquiry honeypot tripped from %s (%s).",
                    _client_ip(request), body.email)
        return InquiryResponse(status="received")

    email = str(body.email).strip().lower()
    first = body.first_name.strip()
    last = body.last_name.strip()
    name = (first + " " + last).strip()
    now = datetime.now(timezone.utc)
    demo_text = body.demo_date.strftime("%Y-%m-%d %H:%M %Z") if body.demo_date else ""

    # The legacy `registrations` table is the inbox: it is what the admin's
    # Demo Registrations page lists, so an inquiry shows up beside the demos
    # entered by hand. Its column names are the CRM's, hence the mapping.
    row = models.Registraion(
        firstname=first,
        lastname=last or "-",
        username=email.split("@")[0],
        useremail=email,
        clientname=body.company.strip() or name,
        servicename=body.interest.strip() or "General inquiry",
        clientemail=email,
        contactphoneno=body.phone.strip() or "-",
        address="",
        demodate=body.demo_date or now,
        createdate=now,
        status="requested",
        notes=body.message.strip() or None,
    )
    db.add(row)
    db.commit()
    db.refresh(row)

    logger.info("Inquiry #%s from %s <%s> (%s), interest %r, ip %s.",
                row.id, name, email, body.company.strip() or "-",
                row.servicename, _client_ip(request))

    background.add_task(
        mailer.send_inquiry_notification,
        name=name, email=email, company=body.company.strip(),
        phone=body.phone.strip(), interest=row.servicename,
        message=body.message.strip(), demo_date=demo_text,
        registration_id=row.id,
    )
    background.add_task(mailer.send_inquiry_acknowledgement, email, first, demo_text)
    return InquiryResponse(status="received", id=row.id)


# ---------------------------------------------------------------------------
# Product updates (section 203). A LIST, not accounts: an address, a
# confirmation click, an unsubscribe link. Grants access to nothing. The
# desk stays behind accounts an admin creates for investors.
#
# Double opt-in is what keeps this honest. Without it the form is a way to
# put anyone's address on a list, and every "update" we ever send is spam to
# them. So: the form stores the row UNCONFIRMED and mails one link; only the
# click sets confirmed_at; nothing but that one mail is ever sent to an
# unconfirmed address. The same token signs the unsubscribe link in every
# later message, which is why it is kept, not rotated, after confirmation.
# ---------------------------------------------------------------------------

CONFIRM_DAYS = 7


class SubscribeRequest(BaseModel):
    email: EmailStr
    name: str = Field(default="", max_length=120)
    interest: str = Field(default="", max_length=120)
    source: str = Field(default="", max_length=40)
    website: str = Field(default="", max_length=200)   # honeypot


class SubscriberOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)   # built from ORM rows

    id: int
    email: str
    name: Optional[str] = None
    interest: Optional[str] = None
    source: Optional[str] = None
    created_at: Optional[datetime] = None
    confirmed_at: Optional[datetime] = None
    unsubscribed_at: Optional[datetime] = None


def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _links(raw: str) -> tuple[str, str]:
    base = mailer.APP_BASE_URL or "https://dataaisys.com"
    return (f"{base}/api/contact/confirm?token={raw}",
            f"{base}/api/contact/unsubscribe?token={raw}")


@router.post("/subscribe", status_code=status.HTTP_202_ACCEPTED)
async def subscribe(body: SubscribeRequest, request: Request,
                    db: db_dependency, background: BackgroundTasks):
    """Always 202 with the same body. Whether the address was new, already
    confirmed, or a bot's, the caller learns nothing about the list."""
    if body.website.strip():
        logger.info("Subscribe honeypot tripped from %s (%s).",
                    _client_ip(request), body.email)
        return {"status": "check_inbox"}

    email = str(body.email).strip().lower()
    now = datetime.now(timezone.utc)
    row = db.query(models.Subscriber).filter(models.Subscriber.email == email).first()

    if row is not None and row.confirmed_at and not row.unsubscribed_at:
        # Already on the list. Say nothing different; send nothing -- a
        # confirmed address must not be mailable by anyone with the form.
        logger.info("Subscribe for already-confirmed %s ignored.", email)
        return {"status": "check_inbox"}

    raw = secrets.token_urlsafe(32)
    if row is None:
        row = models.Subscriber(email=email)
        db.add(row)
    row.name = body.name.strip() or row.name
    row.interest = body.interest.strip() or row.interest
    row.source = body.source.strip() or row.source
    row.token_hash = _hash(raw)
    row.token_expires_at = now + timedelta(days=CONFIRM_DAYS)
    row.unsubscribed_at = None
    row.requested_ip = _client_ip(request)
    db.commit()
    db.refresh(row)

    confirm_url, unsub_url = _links(raw)
    logger.info("Subscribe: #%s %s (%s) awaiting confirmation.", row.id, email,
                row.source or "-")
    background.add_task(mailer.send_subscribe_confirm, email, row.name or "",
                        confirm_url, unsub_url)
    return {"status": "check_inbox"}


def _by_token(db, token: str):
    if not token or len(token) > 200:
        return None
    return db.query(models.Subscriber).filter(
        models.Subscriber.token_hash == _hash(token)).first()


@router.get("/confirm", include_in_schema=False)
async def confirm(token: str, db: db_dependency, background: BackgroundTasks):
    """The click. Lands the visitor back on /contact with a banner."""
    row = _by_token(db, token)
    now = datetime.now(timezone.utc)
    if row is None:
        return RedirectResponse("/contact?updates=invalid", status_code=303)
    if row.confirmed_at is None:
        if row.token_expires_at and row.token_expires_at < now:
            return RedirectResponse("/contact?updates=expired", status_code=303)
        row.confirmed_at = now
        row.unsubscribed_at = None
        db.commit()
        logger.info("Subscriber #%s %s confirmed.", row.id, row.email)
        background.add_task(mailer.send_subscriber_confirmed, email=row.email,
                            name=row.name or "", interest=row.interest or "",
                            source=row.source or "", subscriber_id=row.id)
    elif row.unsubscribed_at is not None:
        # Re-subscribing through an old confirm link: honour it.
        row.unsubscribed_at = None
        db.commit()
    return RedirectResponse("/contact?updates=confirmed", status_code=303)


@router.get("/unsubscribe", include_in_schema=False)
async def unsubscribe(token: str, db: db_dependency):
    """One click, no confirmation page, no login. The token does not expire
    for this purpose: an unsubscribe link in an old mail must keep working."""
    row = _by_token(db, token)
    if row is None:
        return RedirectResponse("/contact?updates=invalid", status_code=303)
    if row.unsubscribed_at is None:
        row.unsubscribed_at = datetime.now(timezone.utc)
        db.commit()
        logger.info("Subscriber #%s %s unsubscribed.", row.id, row.email)
    return RedirectResponse("/contact?updates=unsubscribed", status_code=303)


@router.get("/subscribers", response_model=List[SubscriberOut],
            dependencies=[Depends(require_admin())])
async def list_subscribers(db: db_dependency):
    """Admin only -- the one route under /api/contact that is."""
    return db.query(models.Subscriber).order_by(models.Subscriber.id.desc()).all()

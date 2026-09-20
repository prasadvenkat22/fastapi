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

import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Request, status
from pydantic import BaseModel, EmailStr, Field

import models_pgdb.models as models
from helpers import mailer
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

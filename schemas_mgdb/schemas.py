"""
MongoDB document response schemas.

These Pydantic models describe the shape of documents returned from MongoDB
collections in the e-commerce workflow:

  User → Customer → Device (linked to Customer)
       → ServiceRequest (linked to User + Customer)
       → Transaction (auto-created on ServiceRequest completion)
       → Invoice (groups Transactions for a Customer)
"""

from pydantic import BaseModel, EmailStr, Field
from typing import Optional, List, Dict
from datetime import datetime
from typing import Literal


# ---------------------------------------------------------------------------
# User
# ---------------------------------------------------------------------------

class UserDocument(BaseModel):
    """Shape of a user document stored in MongoDB `users` collection."""
    id: str = Field(..., alias="_id")
    name: str
    email: EmailStr
    passwordHash: Optional[str] = None
    tenantdb: Optional[str] = None
    application: Optional[str] = None
    role: Literal['admin', 'user'] = 'user'
    status: Optional[bool] = False
    imageUrl: Optional[str] = None

    class Config:
        populate_by_name = True


# ---------------------------------------------------------------------------
# Customer
# ---------------------------------------------------------------------------

class ContactDocument(BaseModel):
    name: str
    email: Optional[EmailStr] = None
    phone: Optional[str] = None
    company: Optional[str] = None


class CustomerDocument(BaseModel):
    """Shape of a customer document stored in MongoDB `customers` collection."""
    id: str = Field(..., alias="_id")
    name: str
    status: Literal['active', 'inactive'] = 'active'
    primaryContact: Optional[ContactDocument] = None
    billing: Optional[Dict] = None
    tenantId: Optional[str] = None
    metadata: Optional[Dict] = None
    createdAt: Optional[datetime] = None
    updatedAt: Optional[datetime] = None
    imageUrl: Optional[str] = None

    class Config:
        populate_by_name = True


# ---------------------------------------------------------------------------
# Device (must be linked to a Customer)
# ---------------------------------------------------------------------------

class DeviceDocument(BaseModel):
    """Shape of a device document stored in MongoDB `devices` collection."""
    id: str = Field(..., alias="_id")
    customerId: str                         # required — every device belongs to a customer
    deviceType: Optional[str] = None
    serialNumber: Optional[str] = None
    model: Optional[str] = None
    firmwareVersion: Optional[str] = None
    status: str = 'active'
    lastSeenAt: Optional[datetime] = None
    metadata: Optional[Dict] = None
    createdAt: Optional[datetime] = None
    updatedAt: Optional[datetime] = None
    imageUrl: Optional[str] = None

    class Config:
        populate_by_name = True


# ---------------------------------------------------------------------------
# Service Request
# ---------------------------------------------------------------------------

class ServiceRequestDocument(BaseModel):
    """Shape of a service request document stored in MongoDB `service_requests` collection."""
    id: str = Field(..., alias="_id")
    userId: Optional[str] = None            # MongoDB user _id
    customerId: Optional[str] = None        # MongoDB customer _id
    serviceName: str
    description: str
    status: Literal['pending', 'in_progress', 'completed', 'cancelled'] = 'pending'
    notes: Optional[str] = None
    amount: Optional[float] = None
    createdAt: Optional[datetime] = None
    updatedAt: Optional[datetime] = None
    imageUrl: Optional[str] = None

    class Config:
        populate_by_name = True


# ---------------------------------------------------------------------------
# Transaction (auto-created when ServiceRequest is completed)
# ---------------------------------------------------------------------------

class TransactionDocument(BaseModel):
    """Shape of a transaction document stored in MongoDB `transactions` collection."""
    id: str = Field(..., alias="_id")
    amount: float
    category: str
    description: str
    is_income: bool
    date: str
    userId: Optional[str] = None            # MongoDB user _id
    customerId: Optional[str] = None        # MongoDB customer _id
    serviceRequestId: Optional[str] = None  # originating service request _id
    createdAt: Optional[datetime] = None
    imageUrl: Optional[str] = None

    class Config:
        populate_by_name = True


# ---------------------------------------------------------------------------
# Invoice
# ---------------------------------------------------------------------------

class InvoiceDocument(BaseModel):
    """Shape of an invoice document stored in MongoDB `invoices` collection."""
    id: str = Field(..., alias="_id")
    customer_id: str
    transaction_ids: List[str]
    amount: float
    status: Literal['draft', 'sent', 'paid', 'overdue'] = 'draft'
    createdAt: Optional[datetime] = None
    imageUrl: Optional[str] = None

    class Config:
        populate_by_name = True

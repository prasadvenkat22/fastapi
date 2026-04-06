"""
Pydantic schemas for the Postgres-only e-commerce API.

Workflow: User registers → creates Customer → links Device to Customer
          → places ServiceRequest → Transaction auto-created on completion
          → Invoice generated for Customer
"""
from pydantic import BaseModel, EmailStr, Field, ConfigDict
from typing import List, Optional, Dict, Literal
from datetime import datetime


# ---------------------------------------------------------------------------
# Services / Roles  (kept for existing endpoints)
# ---------------------------------------------------------------------------

class service(BaseModel):
    name: str = Field(..., min_length=2)
    description: str = Field(..., min_length=5)
    DBName: Literal['postgres', 'TenantOne', 'TenantTwo'] = 'postgres'
    createdate: datetime
    imageUrl: Optional[str] = None


class Role(BaseModel):
    role: Literal['user', 'admin'] = 'user'
    desc: str


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

class UserBase(BaseModel):
    email: EmailStr


class ServiceUser(UserBase):
    """Legacy schema used by POST /users/ — kept for backward compat."""
    name: str = Field(..., min_length=3)
    email: EmailStr
    service: str = Field(..., min_length=2)

    model_config = ConfigDict(from_attributes=True)


class UserCreate(BaseModel):
    """POST /register/ — creates a user with a hashed password."""
    name: str = Field(..., min_length=3)
    email: EmailStr
    password: str = Field(..., min_length=6)


class UserResponse(BaseModel):
    id: int
    name: str
    email: str
    created_date: Optional[datetime] = None
    image_url: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


# ---------------------------------------------------------------------------
# Customers
# ---------------------------------------------------------------------------

class CustomerCreate(BaseModel):
    name: str = Field(..., min_length=2)
    status: Literal['active', 'inactive'] = 'active'
    contact_name: Optional[str] = None
    contact_email: Optional[EmailStr] = None
    contact_phone: Optional[str] = None
    billing_address: Optional[str] = None
    tenant_id: Optional[str] = None


class CustomerResponse(CustomerCreate):
    id: int
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    image_url: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


# ---------------------------------------------------------------------------
# Devices  (must be linked to a Customer)
# ---------------------------------------------------------------------------

class DeviceCreate(BaseModel):
    customer_id: int = Field(..., description="ID of the customer this device belongs to")
    device_type: Optional[str] = None
    serial_number: Optional[str] = None
    model: Optional[str] = None
    firmware_version: Optional[str] = None
    status: Optional[str] = 'active'


class DeviceResponse(DeviceCreate):
    id: int
    created_at: Optional[datetime] = None
    image_url: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


# ---------------------------------------------------------------------------
# Service Requests
# ---------------------------------------------------------------------------

class ServiceRequestCreate(BaseModel):
    user_id: int
    customer_id: Optional[int] = None
    service_name: str = Field(..., min_length=2)
    description: str = Field(..., min_length=5)
    status: Literal['pending', 'in_progress', 'completed', 'cancelled'] = 'pending'
    notes: Optional[str] = None
    amount: Optional[float] = None


class ServiceRequestResponse(ServiceRequestCreate):
    id: int
    created_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


class StatusUpdate(BaseModel):
    status: Literal['pending', 'in_progress', 'completed', 'cancelled']


# ---------------------------------------------------------------------------
# Transactions
# ---------------------------------------------------------------------------

class TransactonBase(BaseModel):
    amount: float
    category: str
    description: str
    is_income: bool
    date: str
    user_id: Optional[int] = None
    customer_id: Optional[int] = None


class TransactionModel(TransactonBase):
    id: int

    model_config = ConfigDict(from_attributes=True)


# ---------------------------------------------------------------------------
# Invoices
# ---------------------------------------------------------------------------

class InvoiceCreate(BaseModel):
    customer_id: int
    service_request_id: Optional[int] = None
    amount: float
    status: Literal['draft', 'sent', 'paid', 'overdue'] = 'draft'


class InvoiceResponse(InvoiceCreate):
    id: int
    created_at: Optional[datetime] = None
    image_url: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


# ---------------------------------------------------------------------------
# Products
# ---------------------------------------------------------------------------

class ProductCreate(BaseModel):
    name: str = Field(..., min_length=2)
    description: Optional[str] = None
    price: float = Field(..., gt=0)
    stock: int = Field(0, ge=0)
    category: Optional[str] = None
    sku: Optional[str] = None
    is_active: bool = True


class ProductResponse(ProductCreate):
    id: int
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    image_url: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


# ---------------------------------------------------------------------------
# Registrations  (legacy — kept for backward compat)
# ---------------------------------------------------------------------------

class RegistrationBase(BaseModel):
    firstname: str
    lastname: str
    username: str
    useremail: EmailStr
    clientname: str
    servicename: str
    clientemail: EmailStr
    contactphoneno: str
    address: str
    demodate: datetime
    createdate: datetime
    status: Optional[Literal['requested', 'scheduled', 'completed', 'cancelled']] = 'requested'
    notes: Optional[str] = None


class RegistraionModel(RegistrationBase):
    id: int

    model_config = ConfigDict(from_attributes=True)

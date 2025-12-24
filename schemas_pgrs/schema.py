from pydantic import BaseModel
from typing import List, Annotated, Optional, Dict
from enum import Enum
from fastapi import  Depends
from pydantic import BaseModel, EmailStr,Field
from datetime import datetime
from typing import Literal
#from pydantic_extra_types.phone_numbers import PhoneNumber
class service(BaseModel):
    name:str=Field(..., min_length=2)
    description:str=Field(..., min_length=5)
    DBName: Literal['postgres','TenantOne', 'TenantTwo'] = 'postgres'
    createdate: datetime

class Role(BaseModel):
    role:Literal['user','admin'] = 'user'
    desc:str

class UserBase(BaseModel):
    email: EmailStr

class ServiceUser(UserBase):
    name:str =Field(..., min_length=3)
    #password: str  =Field(..., min_length=3)
    email: EmailStr 
    service:  str  =Field(..., min_length=2)

    class Config:
        from_attributes = True


class TransactonBase(BaseModel) :
    amount:float
    category:str
    description:str
    is_income:bool
    date:str

class TransactionModel(TransactonBase):
    id:int
    class Config:
        from_attributes = True

class RegistrationBase(BaseModel):
    firstname:str
    lastname:str
    username:str
    useremail:EmailStr
    clientname:str
    servicename:str
    clientemail:EmailStr
    contactphoneno:str
    address :str
    demodate:datetime
    createdate:datetime
    # Extended fields for demo booking flow
    customerId: Optional[str]
    serviceId: Optional[int]
    contact: Optional[Dict] = None
    preferredSlots: Optional[List[datetime]] = None
    scheduledSlot: Optional[datetime] = None
    status: Optional[Literal['requested','scheduled','completed','cancelled']] = 'requested'
    assignedToUserId: Optional[str] = None
    source: Optional[str] = None
    notes: Optional[str] = None
class RegistraionModel(RegistrationBase):
    id:int
    class Config:
        from_attributes = True


class Contact(BaseModel):
    name: str
    email: EmailStr
    phone: Optional[str] = None
    company: Optional[str] = None


class CustomerBase(BaseModel):
    name: str = Field(..., min_length=2)
    status: Optional[Literal['active','inactive']] = 'active'
    primaryContact: Optional[Contact] = None
    billing: Optional[Dict] = None
    tenantId: Optional[str] = None
    metadata: Optional[Dict] = None
    createdAt: Optional[datetime] = None
    updatedAt: Optional[datetime] = None


class DeviceBase(BaseModel):
    customerId: Optional[str] = None
    deviceType: Optional[str] = None
    serialNumber: Optional[str] = None
    model: Optional[str] = None
    firmwareVersion: Optional[str] = None
    status: Optional[str] = 'active'
    lastSeenAt: Optional[datetime] = None
    metadata: Optional[Dict] = None
    createdAt: Optional[datetime] = None
    updatedAt: Optional[datetime] = None


class CustomerResponse(CustomerBase):
    _id: Optional[str] = None


class DeviceResponse(DeviceBase):
    _id: Optional[str] = None

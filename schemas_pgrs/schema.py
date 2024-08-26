from pydantic import BaseModel
from typing import List, Annotated
from enum import Enum
from fastapi import FastAPI, HTTPException, Depends
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
class RegistraionModel(RegistrationBase):
    id:int
    class Config:
        from_attributes = True

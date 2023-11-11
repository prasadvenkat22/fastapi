from pydantic import BaseModel
from typing import List, Annotated
from enum import Enum
from fastapi import FastAPI, HTTPException, Depends
from pydantic import BaseModel, EmailStr,Field
from datetime import datetime
from typing import Literal

class Application(BaseModel):
    name:str=Field(..., min_length=2)
    description:str=Field(..., min_length=5)
    DBName: Literal['postgres','TenantOne', 'TenantTwo'] = 'postgres'

class Role(BaseModel):
    role:Literal['user','admin'] = 'user'
    desc:str

class UserBase(BaseModel):
    email: EmailStr

class UserCreate(UserBase):
    password: str

class AppRoleUser(UserBase):
    name:str =Field(..., min_length=3)
    password: str  =Field(..., min_length=3)
    email: EmailStr 
    role: Literal['admin', 'user'] = 'user'
    application:  str  =Field(..., min_length=2)
    class Config:
        from_attributes = True


 
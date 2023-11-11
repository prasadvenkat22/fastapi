from pydantic import BaseModel, Field, EmailStr
import datetime as dt
from typing import Optional,Annotated,Literal
from fastapi import  Query, Body

class user(BaseModel):
    name: str = Field(min_length=1, max_length=16)
    email: EmailStr = Field(default=None)
    password: str =  Field(min_length=1, max_length=15)
    tenantdb:str= Field(min_length=1, max_length=16)
    application:str= Field(min_length=1, max_length=16)
    role: Literal['admin', 'user'] = 'user'
    status: Optional[bool] = False
    date: Annotated[dt.datetime, Query(default_factory=dt.datetime.now)]
    
    #: Annotated[datetime | None, Body()] = None
    #role: AuthRole.WRITE or READ

    class Config:
        json_schema_extra = {
            "example": {
                "name": "xyz",
                "email": "abc@xyz.com",
                "password": "any",
                "tenantdb":"myapp",
                "application":"test"

            }
        }
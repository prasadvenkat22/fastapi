from fastapi import FastAPI, HTTPException,status, Depends, File, UploadFile
from pydantic import BaseModel

from typing import List, Annotated
from schemas_pgrs.schema import ServiceUser,service,Role,TransactionModel,TransactonBase,RegistrationBase,RegistraionModel
import models_pgdb.models as models
from config.db_pgrs import engine, SessionLocal
from sqlalchemy.orm import Session
from sqlalchemy import text
from sqlalchemy.engine import result
import json
import pandas as pd
import time, datetime
from numpy import genfromtxt
from fastapi import APIRouter

router = APIRouter(
    prefix = '/CRUD',
    tags = ['Postgres APIs']
)
from passlib.context import CryptContext
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
class Hasher():
    @staticmethod
    def verify_password(plain_password, hashed_password):
        return pwd_context.verify(plain_password, hashed_password)
    @staticmethod
    def get_password_hash(password):
        return pwd_context.hash(password)
    
models.Base.metadata.create_all(bind=engine)
def get_db():
    db= SessionLocal()
    try:
        yield db
    finally:
        db.close()

db_dependency= Annotated[Session, Depends(get_db)]

@router.get("/users/")
async def get_users(db: db_dependency, skip: int = 0, limit: int = 100):
    result= db.query(models.User).offset(skip).limit(limit).all()
    if not result:
        raise HTTPException(status_code=404, detail='Users not found')
    return result

@router.get("/users/{user_id}")
async def get_user_id(user_id: int, db:db_dependency):
    result=db.query(models.User).filter(models.User.id==user_id).first()
    if not result:
        raise HTTPException(status_code=404, detail='User ID not found')
    return result
@router.get("/services/{service_id}")
async def get_application_id(service_id:int, db:db_dependency):
    result=db.query(models.Service).filter(models.Service.id==service_id).first()
    if not result:
        raise HTTPException(status_code=404, detail='service_id  not found')
    return result

@router.get("/services/")
async def get_applications(db: db_dependency, skip: int = 0, limit: int = 100):
    return db.query(models.Service).offset(skip).limit(limit).all()

@router.get("/roles/")
async def get_roles(db: db_dependency, skip: int = 0, limit: int = 100):
    return db.query(models.Role).offset(skip).limit(limit).all()


@router.get("/transactions/")
async def get_transactions(db: db_dependency, skip: int = 0, limit: int = 100):
    return db.query(models.Transaction).offset(skip).limit(limit).all()

@router.get("/registrations/")
async def get_transactions(db: db_dependency, skip: int = 0, limit: int = 100):
    return db.query(models.Registraion).offset(skip).limit(limit).all()


@router.post("/users/")
async def create_user(Usr: ServiceUser, db: db_dependency):
    try:
       # hashed_pwd = Hasher.get_password_hash(Usr.password)

        db_user = models.User(name=Usr.name,
                           #hashed_password=hashed_pwd ,
                           email=Usr.email, 
                           service = Usr.service, 
                           servicedemodate=Usr.servicedemodate)
        result=db.query(models.User).filter(models.User.name==Usr.name).filter(models.User.service == Usr.service).first()
        #if result:
            #return {"error": f"User by that Name and service Exists, {Usr.name} - {Usr.application}"}
        # resultapp=db.query(models.Service).filter(models.Service.name==Usr.service).first()
        # if not resultapp:
        #     return {"error": f"service by that Name Does not Exists-please add the servive first and retry,  {Usr.application}"}
        # print(db_user.hashed_password)
        # print(db_user.application)
        # print(db_user.role)
        # print(db_user.email)
        # print(db_user.name)
        # print(db_user.service)
        # print(db_user.servicedemodate)

        # if not result:
        db.add(db_user)
        db.commit()
        db.refresh(db_user)
        #result= create_db_users(Usr.name,Usr.password,resultapp.DBName,Usr.role)
        return {"status": "User added to Database", "User": db_user}
        #else:
            #return {"error": f"User by that Name Exists {Usr.name}"}
    except Exception:
       raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Input Data validation / constraint error")
 
db_dependency= Annotated[Session, Depends(get_db)]
@router.post("/services/")
async def create_app(Svc: service, db: db_dependency):
    #  sd-app=models.Applications(**App.dict())
    try:
        db_svc = models.Service(
            name=Svc.name, 
            description=Svc.description, 
            DBName = Svc.DBName)
        result=db.query(models.Service).filter(models.Service.name==Svc.name).first()
        if not result:
            db.add(db_svc)
            db.commit()
            db.refresh(db_svc)
            return {"status": "Svc added to Database", "App": db_svc}
        else:
            return {"error": f"Svc by that Name Exists {db_svc.name}"}
    except Exception:
       raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Input Data validation / constraint error")
@router.post("/roles/")
async def create_role(r: Role, db: db_dependency):
    #  sd-app=models.Applications(**App.dict())
    try:
        db_role = models.Role(role=r.role, description=r.desc)
        result=db.query(models.Role).filter(models.Role.role==r.role).first()
        if not result:
             db.add(db_role)
             db.commit()
             db.refresh(db_role)
             return {"status": "Role added to Database", "Role": db_role}
        else:
            return {"error": f"Role by that Name Exists {r.role}"}
    except Exception:
       raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Input Data validation / constraint error")
@router.post("/transactions/")
async def create_transaction(transaction:TransactonBase, db: db_dependency):
    #  sd-app=models.Applications(**App.dict())
    try: 
        db_Trasaction = models.Transaction(**transaction.model_dump())
        db.add(db_Trasaction)
        db.commit()
        db.refresh(db_Trasaction)
        return {"status": "Transaction added to Database", "Transaction": db_Trasaction}
    except Exception:
       raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Input Data validation / constraint error")
@router.post("/registrations/")
async def create_registration(registration:RegistrationBase, db: db_dependency):
    #  sd-app=models.Applications(**App.dict())
    try:
        db_Registration = models.Registraion(**registration.model_dump())
        db.add(db_Registration)
        db.commit()
        db.refresh(db_Registration)
        return {"status": "registration added to Database", "Registratoin": db_Registration}

    except Exception:
       raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Input Data registraion validation / constraint error")

def create_db_users(user, password, tenantdbname,role):
# write the SQL query inside the text() block
    try:
        with engine.connect() as conn:
            user=user
            pwd=password
            role=role
            json_ = json.dumps({'x': user})
            if role=='user':
                sql = text( 'CREATE USER '+ user + ' WITH PASSWORD :p ')
            else:
                sql = text( 'CREATE USER '+ user + ' WITH SUPERUSER	NOCREATEDB 	CREATEROLE 	INHERIT NOREPLICATION PASSWORD :p ' )

            print(sql)
            result = conn.execute(sql,{'p':pwd })
            conn.commit()
            with engine.connect() as conn:
                stmt= text("select * from pg_catalog.pg_user where usename = :u")
                
                for row in conn.execute(stmt, {'u':user}):
                    print(f"{row.usename}")

        return {"status": "User added to Database", "User": user }
    except Exception:
       raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Input Data validation / constraint error")

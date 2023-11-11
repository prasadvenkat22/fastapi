from fastapi import FastAPI, HTTPException,status, Depends, File, UploadFile
from typing import List, Annotated
from schemas_pgrs.schema import AppRoleUser,Application,UserCreate,Role
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
    return db.query(models.User).offset(skip).limit(limit).all()
@router.get("/users/{user_id}")
async def get_user_id(user_id: int, db:db_dependency):
    result=db.query(models.User).filter(models.User.id==user_id).first()
    if not result:
        raise HTTPException(status_code=404, detail='User ID not found')
    return result
@router.get("/applications/{application_id}")
async def get_application_id(application_id:int, db:db_dependency):
    result=db.query(models.Application).filter(models.Application.id==application_id).first()
    if not result:
        raise HTTPException(status_code=404, detail='User ID not found')
    return result

@router.get("/applications/")
async def get_applications(db: db_dependency, skip: int = 0, limit: int = 100):
    return db.query(models.Application).offset(skip).limit(limit).all()

@router.get("/roles/")
async def get_roles(db: db_dependency, skip: int = 0, limit: int = 100):
    return db.query(models.Role).offset(skip).limit(limit).all()

@router.post("/users/")
async def create_user(Usr: AppRoleUser, db: db_dependency):
    try:
        hashed_pwd = Hasher.get_password_hash(Usr.password)

        db_user = models.User(name=Usr.name,
                           hashed_password=hashed_pwd ,
                           email=Usr.email, role=Usr.role,
                           application = Usr.application)
        result=db.query(models.User).filter(models.User.name==Usr.name).filter(models.User.application == Usr.application).first()
        if result:
            return {"error": f"User by that Name and APP Exists, {Usr.name} - {Usr.application}"}
        resultapp=db.query(models.Application).filter(models.Application.name==Usr.application).first()
        if not resultapp:
            return {"error": f"Applicaiton by that Name Does not Exists-please add the App first and retry,  {Usr.application}"}
        print(db_user.hashed_password)
        print(db_user.application)
        print(db_user.role)
        print(db_user.email)
        print(db_user.name)
        if not result:
            db.add(db_user)
            db.commit()
            db.refresh(db_user)
            #result= create_db_users(Usr.name,Usr.password,resultapp.DBName,Usr.role)
            return {"status": "User added to Database", "User": db_user}
        else:
            return {"error": f"User by that Name Exists {Usr.name}"}
    except Exception:
       raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Input Data validation / constraint error")
 
db_dependency= Annotated[Session, Depends(get_db)]
@router.post("/applications/")
async def create_app(App: Application, db: db_dependency):
    #  sd-app=models.Applications(**App.dict())
    try:
        db_app = models.Application(
            name=App.name, 
            description=App.description,
            DBName = App.DBName)
        result=db.query(models.Application).filter(models.Application.name==App.name).first()
        if not result:
            db.add(db_app)
            db.commit()
            db.refresh(db_app)
            return {"status": "App added to Database", "App": db_app}
        else:
            return {"error": f"App by that Name Exists {db_app.name}"}
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

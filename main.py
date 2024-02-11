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
from passlib.context import CryptContext
import routes.db_pgrs_router as db_pgrs_router,routes.dbfile_pgrs_router as dbfile_pgrs_router 
import routes.ML.image_router as image_router
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi import FastAPI
import uvicorn
from fastapi.staticfiles import StaticFiles
import routes.db_mngdb_router as db_mngdb_router

app = FastAPI(title="FastAPI-Users-Backend",description = "CRUD API")

app.mount("/static", StaticFiles(directory="static"), name="static")

app = FastAPI()
origins = ["*"]
methods = ["*"]
headers = ["*"]
app.add_middleware(
    CORSMiddleware, 
    allow_origins = origins,
    allow_credentials = True,
    allow_methods = methods,
    allow_headers = headers    
)

app.include_router(db_mngdb_router.router)#,tags=['Users'], prefix='/api/mongodb')
app.include_router(db_pgrs_router.router)#,tags=['Users'], prefix='/api/postgres')
app.include_router(dbfile_pgrs_router.router)#,tags=['files'], prefix='/api/files')
#app.include_router(image_router.router)
#app.include_router(image_router.router)

#models.Base.metadata.create_all(bind=engine)
# def get_db():
#     db= SessionLocal()
#     try:
#         yield db
#     finally:
#         db.close()
@app.get("/")
async def home():
    return {"Home": "FastAPI Univied MongoDB and Postgresql"}
#db_dependency= Annotated[Session, Depends(get_db)]
app.mount("/static", StaticFiles(directory="static"), name="static")


from fastapi import FastAPI, HTTPException,status, Depends, File, UploadFile
from typing import List, Annotated
from schemas_pgrs.schema import service,ServiceUser,UserBase,Role,RegistraionModel
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
import uvicorn
import routes.db_mngdb_router as db_mngdb_router
import GENAI.router as genai_router
import models_mgdb.db as mgdb

app = FastAPI(title="FastAPI-Users-Backend",description = "CRUD API")



origins = ["*"]

methods = ["*"]

headers = ["*"]

app.add_middleware(

    CORSMiddleware,

    allow_origins = origins,

    allow_credentials = False,

    allow_methods = methods,

    allow_headers = headers

)



app.mount("/static", StaticFiles(directory="static"), name="static")





app.include_router(db_mngdb_router.router)#,tags=['Users'], prefix='/api/mongodb')

app.include_router(db_pgrs_router.router)#,tags=['Users'], prefix='/api/postgres')

app.include_router(dbfile_pgrs_router.router)#,tags=['files'], prefix='/api/files')

app.include_router(genai_router.router)

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





@app.on_event("startup")

async def on_startup():

    """Initialize Mongo client and create Postgres tables (if reachable)."""

    try:

        await mgdb.init_db()  # Initialize Mongo (Motor) client

    except Exception:

        # don't crash app on startup if Mongo is not available; log a warning

        import logging

        logging.getLogger(__name__).warning("Mongo init_db failed on startup")



    try:

        from config.db_pgrs import engine

        import models_pgdb.models as models

        models.Base.metadata.create_all(bind=engine)

    except Exception as e:

        import logging

        logging.getLogger(__name__).warning("Postgres create_all failed on startup: %s", e)
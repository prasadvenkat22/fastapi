from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
# Using python-dotenv to Load Env variables
from dotenv import load_dotenv
import os

load_dotenv()
DATABASE_URL_POSTRESS = os.getenv("DATABASE_URL")

engine = create_engine(DATABASE_URL_POSTRESS)
SessionLocal =  sessionmaker(autoflush=False, autocommit=False, bind= engine)

Base = declarative_base()


from sqlalchemy import Column, Integer, String,Boolean,ForeignKey,DateTime,UniqueConstraint,Date,Float
from config.db_pgrs import Base
from sqlalchemy.orm import relationship
from datetime import datetime
from sqlalchemy.sql import func

class Application(Base):
    __tablename__ = "application"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String, unique=True,default='App')
    description = Column(String, index=True)
    created_date =  Column(DateTime(timezone=True), default=func.now())
    disabled = Column(Boolean, default=False)
    DBName = Column(String, default='postgres')
    UniqueConstraint ('name', 'DBName', name='uix_2')

class Role(Base):
    __tablename__ = "roles"

    id = Column(Integer, primary_key=True, autoincrement=True)
    role = Column(String, unique=True,default='user')
    description = Column(String, index=True)

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String,  nullable=False)
    hashed_password = Column(String)
    email = Column(String,   nullable=False)
    created_date =  Column(DateTime(timezone=True), default=func.now())
    disabled = Column(Boolean, default=False)
    role =Column(String )
    application =Column(String)
    UniqueConstraint ('name', 'application', name='uix_1')

#    user_id = Column(Integer, ForeignKey(User.id), primary_key=True)


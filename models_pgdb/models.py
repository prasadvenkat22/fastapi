from sqlalchemy import Column, Integer, String,Boolean,ForeignKey,DateTime,UniqueConstraint,Date,Float
from config.db_pgrs import Base
from sqlalchemy.orm import relationship
from datetime import datetime
from sqlalchemy.sql import func

class Service(Base):
    __tablename__ = "services"
    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String, unique=True,default='App')
    description = Column(String, index=True)
    createdate =  Column(DateTime(timezone=True), default=func.now())
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
    email = Column(String,   nullable=False)
    created_date =  Column(DateTime(timezone=True), default=func.now())
    disabled = Column(Boolean, default=False)
    UniqueConstraint ('name', 'service', name='uix_1')

#    user_id = Column(Integer, ForeignKey(User.id), primary_key=True)

class Transaction(Base):
    __tablename__ = "transactions"
    id = Column(Integer,primary_key= True, index= True)
    amount = Column(Float)
    category = Column(String)
    description = Column(String)
    is_income = Column(Boolean)
    date= Column(String)

class Registraion(Base):
    __tablename__ = "registrations"
    id = Column(Integer,primary_key= True, index= True)
    firstname=Column(String)
    lastname=Column(String)
    username = Column(String)
    useremail = Column(String)
    clientname = Column(String)
    servicename = Column(String)
    clientemail = Column(String)
    contactphoneno = Column(String)
    address = Column(String)
    demodate= Column(DateTime)
    createdate= Column(DateTime)


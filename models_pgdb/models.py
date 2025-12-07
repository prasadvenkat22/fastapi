from sqlalchemy import Column, Integer, String,Boolean,ForeignKey,DateTime,UniqueConstraint,Date,Float
from config.db_pgrs import Base
from sqlalchemy.orm import relationship
from datetime import datetime
from sqlalchemy.sql import func

class Service(Base):
    __tablename__ = "services"
    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String, unique=True,default='TestService')
    description = Column(String, index= True,default='This is a test service')
    createdate =  Column(DateTime(timezone=True), default=func.now())
    disabled = Column(Boolean, default=False)
    DBName = Column(String, default='postgres')
    #UniqueConstraint ('name', 'DBName', name='uix_2')

class Role(Base):
    __tablename__ = "roles"

    id = Column(Integer, primary_key=True, autoincrement=True)
    role = Column(String, unique=True,default='user')
    description = Column(String, index=True)

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String,  nullable=False,default   ='TestUser')
    email = Column(String,   nullable=False,default="TestEmail@test.com")
    created_date =  Column(DateTime(timezone=True), default=func.now())
    disabled = Column(Boolean, default=False)
    #UniqueConstraint ('name', 'service', name='uix_1')

#    user_id = Column(Integer, ForeignKey(User.id), primary_key=True)

class Transaction(Base):
    __tablename__ = "transactions"
    id = Column(Integer,primary_key= True, index= True)
    amount = Column(Float,default=0.0)
    category = Column(String,default='General')
    description = Column(String,default='Test Description')
    is_income = Column(Boolean,default= True) 
    date= Column(String,default=str(datetime.now().date()))

class Registraion(Base):
    __tablename__ = "registrations"
    id = Column(Integer,primary_key= True, index= True)
    firstname=Column(String,default='Test')
    lastname=Column(String,default='TestLastName')
    username = Column(String,default='testuser')
    useremail = Column(String,  default="Test@testemail.com")
    clientname = Column(String,default="testClient")
    servicename = Column(String,default='TestService')
    clientemail = Column(String,default="testCelienEmail")
    contactphoneno = Column(String,default='1234567890')
    address = Column(String,default='Test Address')
    demodate= Column(DateTime,default=func.now())
    createdate= Column(DateTime,default=func.now())

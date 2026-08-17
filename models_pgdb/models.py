from sqlalchemy import Column, Integer, String, Boolean, ForeignKey, DateTime, Float
from config.db_pgrs import Base
from sqlalchemy.orm import relationship
from datetime import datetime
from sqlalchemy.sql import func


class Service(Base):
    __tablename__ = "services"
    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String, unique=True, default='TestService')
    description = Column(String, index=True, default='This is a test service')
    createdate = Column(DateTime(timezone=True), default=func.now())
    disabled = Column(Boolean, default=False)
    DBName = Column(String, default='postgres')
    image_url = Column(String, nullable=True)


class Role(Base):
    __tablename__ = "roles"
    id = Column(Integer, primary_key=True, autoincrement=True)
    role = Column(String, unique=True, default='user')
    description = Column(String, index=True)


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String, nullable=False, default='TestUser')
    email = Column(String, nullable=False, default="TestEmail@test.com")
    password_hash = Column(String, nullable=True)
    created_date = Column(DateTime(timezone=True), default=func.now())
    disabled = Column(Boolean, default=False)
    image_url = Column(String, nullable=True)
    # Nullable on purpose. The roles table existed with its own CRUD routes
    # but nothing referenced it, so no user has ever held a role — a NOT NULL
    # column would have to invent one for every existing row. NULL means "no
    # role assigned", which authorization must read as no permissions rather
    # than as a default grant.
    role_id = Column(Integer, ForeignKey("roles.id"), nullable=True, index=True)

    service_requests = relationship("ServiceRequest", back_populates="user")
    transactions = relationship("Transaction", back_populates="user")
    role = relationship("Role")


class Customer(Base):
    __tablename__ = "customers"
    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String, nullable=False)
    status = Column(String, default='active')
    contact_name = Column(String, nullable=True)
    contact_email = Column(String, nullable=True)
    contact_phone = Column(String, nullable=True)
    billing_address = Column(String, nullable=True)
    tenant_id = Column(String, nullable=True)
    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now())
    image_url = Column(String, nullable=True)

    devices = relationship("Device", back_populates="customer")
    service_requests = relationship("ServiceRequest", back_populates="customer")
    transactions = relationship("Transaction", back_populates="customer")
    invoices = relationship("Invoice", back_populates="customer")


class Device(Base):
    __tablename__ = "devices"
    id = Column(Integer, primary_key=True, autoincrement=True)
    customer_id = Column(Integer, ForeignKey("customers.id"), nullable=False)
    device_type = Column(String, nullable=True)
    serial_number = Column(String, nullable=True)
    model = Column(String, nullable=True)
    firmware_version = Column(String, nullable=True)
    status = Column(String, default='active')
    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now())
    image_url = Column(String, nullable=True)

    customer = relationship("Customer", back_populates="devices")


class ServiceRequest(Base):
    __tablename__ = "service_requests"
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    customer_id = Column(Integer, ForeignKey("customers.id"), nullable=True)
    service_name = Column(String, nullable=False)
    description = Column(String, nullable=False)
    status = Column(String, default='pending')
    notes = Column(String, nullable=True)
    amount = Column(Float, nullable=True)
    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now())

    user = relationship("User", back_populates="service_requests")
    customer = relationship("Customer", back_populates="service_requests")
    invoices = relationship("Invoice", back_populates="service_request")


class Transaction(Base):
    __tablename__ = "transactions"
    id = Column(Integer, primary_key=True, index=True)
    amount = Column(Float, default=0.0)
    category = Column(String, default='General')
    description = Column(String, default='Test Description')
    is_income = Column(Boolean, default=True)
    date = Column(String, default=str(datetime.now().date()))
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    customer_id = Column(Integer, ForeignKey("customers.id"), nullable=True)

    user = relationship("User", back_populates="transactions")
    customer = relationship("Customer", back_populates="transactions")


class Invoice(Base):
    __tablename__ = "invoices"
    id = Column(Integer, primary_key=True, autoincrement=True)
    customer_id = Column(Integer, ForeignKey("customers.id"), nullable=False)
    service_request_id = Column(Integer, ForeignKey("service_requests.id"), nullable=True)
    amount = Column(Float, nullable=False)
    status = Column(String, default='draft')
    created_at = Column(DateTime, default=func.now())
    image_url = Column(String, nullable=True)

    customer = relationship("Customer", back_populates="invoices")
    service_request = relationship("ServiceRequest", back_populates="invoices")


class Product(Base):
    __tablename__ = "products"
    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String, nullable=False)
    description = Column(String, nullable=True)
    price = Column(Float, nullable=False)
    stock = Column(Integer, default=0)
    category = Column(String, nullable=True)
    sku = Column(String, unique=True, nullable=True)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now())
    image_url = Column(String, nullable=True)


class EntityImage(Base):
    """One row per uploaded image, supporting multiple images per entity record
    (e.g. several photos for one product/property). entity_type/entity_id form a
    polymorphic reference — no FK constraint, since entity_type spans several
    tables (products, customers, users, ...)."""
    __tablename__ = "entity_images"
    id = Column(Integer, primary_key=True, autoincrement=True)
    entity_type = Column(String, nullable=False, index=True)
    entity_id = Column(Integer, nullable=False, index=True)
    image_url = Column(String, nullable=False)
    sort_order = Column(Integer, default=0)
    created_at = Column(DateTime(timezone=True), default=func.now())


class Registraion(Base):
    __tablename__ = "registrations"
    id = Column(Integer, primary_key=True, index=True)
    firstname = Column(String, default='Test')
    lastname = Column(String, default='TestLastName')
    username = Column(String, default='testuser')
    useremail = Column(String, default="Test@testemail.com")
    clientname = Column(String, default="testClient")
    servicename = Column(String, default='TestService')
    clientemail = Column(String, default="testCelienEmail")
    contactphoneno = Column(String, default='1234567890')
    address = Column(String, default='Test Address')
    demodate = Column(DateTime, default=func.now())
    createdate = Column(DateTime, default=func.now())

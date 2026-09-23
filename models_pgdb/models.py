from sqlalchemy import Column, Integer, String, Boolean, ForeignKey, DateTime, Float, Text
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
    # unique: email is the login identifier, so two rows sharing one would
    # make authentication ambiguous.
    email = Column(String, nullable=False, unique=True, default="TestEmail@test.com")
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
    # True from public sign-up until the emailed link is opened; login refuses
    # it. A flag rather than a verified-at date so that every account created
    # before sign-up existed (all admin-made) stays valid without a backfill.
    pending_verification = Column(Boolean, nullable=False, default=False,
                                  server_default="false")

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
    # Added 2026-09-20 (alembic a9c4e17b52d3). The schema and the site's Demo
    # Registrations page had both all along; the table did not, so the CRUD
    # route silently dropped them. The public contact form keeps the
    # visitor's message in `notes`.
    status = Column(String, default='requested')
    notes = Column(Text, nullable=True)


class EmailVerificationToken(Base):
    """The link a public sign-up must open before the account can log in.

    Same shape and the same reasoning as PasswordResetToken below: sha256 of
    the raw token, kept after use so a second click gets an explanation.
    """
    __tablename__ = "email_verification_tokens"
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"),
                     nullable=False, index=True)
    token_hash = Column(String, nullable=False, unique=True, index=True)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    used_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=func.now())
    requested_ip = Column(String, nullable=True)


class PasswordResetToken(Base):
    """A one-time, short-lived permission to choose a new password.

    HASHED, like a password, because that is exactly what it is for the
    minutes it lives: anyone holding the raw value can take the account. The
    column stores sha256 of the token, so a copy of this table is not a set of
    working reset links.

    sha256 rather than bcrypt here on purpose. bcrypt is slow BY DESIGN to
    make guessing a human-chosen password expensive; these are 32 bytes of
    secrets.token_urlsafe, so there is nothing to guess and the cost would buy
    nothing but a slow endpoint.

    Rows are kept after use rather than deleted, so "this link was already
    used" is answerable and a second click gets a real explanation.
    """
    __tablename__ = "password_reset_tokens"
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"),
                     nullable=False, index=True)
    token_hash = Column(String, nullable=False, unique=True, index=True)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    used_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=func.now())
    # What asked for it. Not enforcement -- just enough to see a pattern if
    # the endpoint is ever abused, since it is public by necessity.
    requested_ip = Column(String, nullable=True)

    user = relationship("User")


class Subscriber(Base):
    """Someone who asked for product updates. NOT an account (section 203).

    No password, no role, nothing unlocked: a row here grants access to
    nothing, which is the point -- the desk stays behind accounts an admin
    creates for investors. Double opt-in: `confirmed_at` is set only when the
    address clicks the link we mailed it, so a form filled in with someone
    else's address subscribes nobody. `token_hash` is sha256 of the token in
    that link (same reasoning as PasswordResetToken), and the same token
    signs every unsubscribe link, so it is kept rather than rotated.
    """
    __tablename__ = "subscribers"
    id = Column(Integer, primary_key=True, autoincrement=True)
    email = Column(String, nullable=False, unique=True, index=True)
    name = Column(String, nullable=True)
    interest = Column(String, nullable=True)
    source = Column(String, nullable=True)          # footer / contact / ...
    token_hash = Column(String, nullable=False, unique=True, index=True)
    token_expires_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=func.now())
    confirmed_at = Column(DateTime(timezone=True), nullable=True)
    unsubscribed_at = Column(DateTime(timezone=True), nullable=True)
    requested_ip = Column(String, nullable=True)

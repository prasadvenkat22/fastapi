import logging
import datetime
import bcrypt as _bcrypt

from fastapi import APIRouter, HTTPException, status, Depends
from typing import List, Annotated
from sqlalchemy.orm import Session

from config.db_pgrs import engine, SessionLocal
import models_pgdb.models as models
from schemas_pgrs.schema import (
    ServiceUser, service, Role,
    TransactonBase, TransactionModel,
    RegistrationBase, RegistraionModel,
    UserCreate, UserResponse,
    CustomerCreate, CustomerResponse,
    DeviceCreate, DeviceResponse,
    ServiceRequestCreate, ServiceRequestResponse, StatusUpdate,
    InvoiceCreate, InvoiceResponse,
    ProductCreate, ProductResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix='/CRUD', tags=['Postgres APIs'])


# ---------------------------------------------------------------------------
# Password hashing
# ---------------------------------------------------------------------------

class Hasher:
    @staticmethod
    def verify_password(plain: str, hashed: str) -> bool:
        return _bcrypt.checkpw(plain.encode('utf-8'), hashed.encode('utf-8'))

    @staticmethod
    def get_password_hash(password: str) -> str:
        return _bcrypt.hashpw(password.encode('utf-8'), _bcrypt.gensalt()).decode('utf-8')


# ---------------------------------------------------------------------------
# DB session dependency
# ---------------------------------------------------------------------------

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


db_dependency = Annotated[Session, Depends(get_db)]


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

@router.post("/register/", response_model=UserResponse)
async def register_user(usr: UserCreate, db: db_dependency):
    if db.query(models.User).filter(models.User.email == usr.email).first():
        raise HTTPException(status_code=400, detail="Email already registered")
    db_user = models.User(
        name=usr.name,
        email=usr.email,
        password_hash=Hasher.get_password_hash(usr.password),
    )
    db.add(db_user)
    db.commit()
    db.refresh(db_user)
    return db_user


@router.get("/users/", response_model=List[UserResponse])
async def get_users(db: db_dependency, skip: int = 0, limit: int = 100):
    users = db.query(models.User).offset(skip).limit(limit).all()
    if not users:
        raise HTTPException(status_code=404, detail='No users found')
    return users


@router.post("/users/", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def create_user(usr: UserCreate, db: db_dependency):
    if db.query(models.User).filter(models.User.email == usr.email).first():
        raise HTTPException(status_code=400, detail="Email already registered")
    db_user = models.User(
        name=usr.name,
        email=usr.email,
        password_hash=Hasher.get_password_hash(usr.password),
    )
    db.add(db_user)
    db.commit()
    db.refresh(db_user)
    return db_user


@router.get("/users/{user_id}", response_model=UserResponse)
async def get_user(user_id: int, db: db_dependency):
    user = db.query(models.User).filter(models.User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail='User not found')
    return user


@router.delete("/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user(user_id: int, db: db_dependency):
    user = db.query(models.User).filter(models.User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail='User not found')
    db.delete(user)
    db.commit()


# ---------------------------------------------------------------------------
# Customers
# ---------------------------------------------------------------------------

@router.post("/customers/", response_model=CustomerResponse)
async def create_customer(data: CustomerCreate, db: db_dependency):
    db_customer = models.Customer(**data.model_dump())
    db.add(db_customer)
    db.commit()
    db.refresh(db_customer)
    return db_customer


@router.get("/customers/", response_model=List[CustomerResponse])
async def get_customers(db: db_dependency, skip: int = 0, limit: int = 100):
    return db.query(models.Customer).offset(skip).limit(limit).all()


@router.get("/customers/{customer_id}", response_model=CustomerResponse)
async def get_customer(customer_id: int, db: db_dependency):
    customer = db.query(models.Customer).filter(models.Customer.id == customer_id).first()
    if not customer:
        raise HTTPException(status_code=404, detail='Customer not found')
    return customer


@router.put("/customers/{customer_id}", response_model=CustomerResponse)
async def update_customer(customer_id: int, data: CustomerCreate, db: db_dependency):
    customer = db.query(models.Customer).filter(models.Customer.id == customer_id).first()
    if not customer:
        raise HTTPException(status_code=404, detail='Customer not found')
    for field, value in data.model_dump().items():
        setattr(customer, field, value)
    customer.updated_at = datetime.datetime.utcnow()
    db.commit()
    db.refresh(customer)
    return customer


@router.delete("/customers/{customer_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_customer(customer_id: int, db: db_dependency):
    customer = db.query(models.Customer).filter(models.Customer.id == customer_id).first()
    if not customer:
        raise HTTPException(status_code=404, detail='Customer not found')
    db.delete(customer)
    db.commit()


# ---------------------------------------------------------------------------
# Devices
# ---------------------------------------------------------------------------

@router.post("/devices/", response_model=DeviceResponse)
async def create_device(data: DeviceCreate, db: db_dependency):
    customer = db.query(models.Customer).filter(models.Customer.id == data.customer_id).first()
    if not customer:
        raise HTTPException(status_code=404, detail=f'Customer {data.customer_id} not found')
    db_device = models.Device(**data.model_dump())
    db.add(db_device)
    db.commit()
    db.refresh(db_device)
    return db_device


@router.get("/devices/", response_model=List[DeviceResponse])
async def get_devices(db: db_dependency, skip: int = 0, limit: int = 100):
    return db.query(models.Device).offset(skip).limit(limit).all()


@router.get("/devices/{device_id}", response_model=DeviceResponse)
async def get_device(device_id: int, db: db_dependency):
    device = db.query(models.Device).filter(models.Device.id == device_id).first()
    if not device:
        raise HTTPException(status_code=404, detail='Device not found')
    return device


@router.get("/customers/{customer_id}/devices", response_model=List[DeviceResponse])
async def get_devices_by_customer(customer_id: int, db: db_dependency):
    customer = db.query(models.Customer).filter(models.Customer.id == customer_id).first()
    if not customer:
        raise HTTPException(status_code=404, detail='Customer not found')
    return db.query(models.Device).filter(models.Device.customer_id == customer_id).all()


@router.delete("/devices/{device_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_device(device_id: int, db: db_dependency):
    device = db.query(models.Device).filter(models.Device.id == device_id).first()
    if not device:
        raise HTTPException(status_code=404, detail='Device not found')
    db.delete(device)
    db.commit()


# ---------------------------------------------------------------------------
# Service Requests
# ---------------------------------------------------------------------------

@router.post("/service-requests/", response_model=ServiceRequestResponse)
async def create_service_request(req: ServiceRequestCreate, db: db_dependency):
    if not db.query(models.User).filter(models.User.id == req.user_id).first():
        raise HTTPException(status_code=404, detail=f'User {req.user_id} not found')
    if req.customer_id and not db.query(models.Customer).filter(models.Customer.id == req.customer_id).first():
        raise HTTPException(status_code=404, detail=f'Customer {req.customer_id} not found')
    db_req = models.ServiceRequest(**req.model_dump())
    db.add(db_req)
    db.commit()
    db.refresh(db_req)
    return db_req


@router.get("/service-requests/", response_model=List[ServiceRequestResponse])
async def get_service_requests(db: db_dependency, skip: int = 0, limit: int = 100):
    return db.query(models.ServiceRequest).offset(skip).limit(limit).all()


@router.get("/service-requests/{id}", response_model=ServiceRequestResponse)
async def get_service_request(id: int, db: db_dependency):
    req = db.query(models.ServiceRequest).filter(models.ServiceRequest.id == id).first()
    if not req:
        raise HTTPException(status_code=404, detail='Service request not found')
    return req


@router.put("/service-requests/{id}/status")
async def update_service_request_status(id: int, status_update: StatusUpdate, db: db_dependency):
    req = db.query(models.ServiceRequest).filter(models.ServiceRequest.id == id).first()
    if not req:
        raise HTTPException(status_code=404, detail='Service request not found')
    req.status = status_update.status
    req.updated_at = datetime.datetime.utcnow()
    # Auto-create a transaction on completion if amount is set
    if status_update.status == 'completed' and req.amount:
        db.add(models.Transaction(
            amount=req.amount,
            category='Service',
            description=f'Completed: {req.service_name}',
            is_income=True,
            date=str(datetime.datetime.utcnow().date()),
            user_id=req.user_id,
            customer_id=req.customer_id,
        ))
    db.commit()
    return {"status": "updated", "service_request_id": id, "new_status": status_update.status}


@router.delete("/service-requests/{id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_service_request(id: int, db: db_dependency):
    req = db.query(models.ServiceRequest).filter(models.ServiceRequest.id == id).first()
    if not req:
        raise HTTPException(status_code=404, detail='Service request not found')
    db.delete(req)
    db.commit()


# ---------------------------------------------------------------------------
# Transactions
# ---------------------------------------------------------------------------

@router.post("/transactions/", response_model=TransactionModel)
async def create_transaction(transaction: TransactonBase, db: db_dependency):
    db_tx = models.Transaction(**transaction.model_dump())
    db.add(db_tx)
    db.commit()
    db.refresh(db_tx)
    return db_tx


@router.get("/transactions/", response_model=List[TransactionModel])
async def get_transactions(db: db_dependency, skip: int = 0, limit: int = 100):
    return db.query(models.Transaction).offset(skip).limit(limit).all()


@router.delete("/transactions/{transaction_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_transaction(transaction_id: int, db: db_dependency):
    tx = db.query(models.Transaction).filter(models.Transaction.id == transaction_id).first()
    if not tx:
        raise HTTPException(status_code=404, detail='Transaction not found')
    db.delete(tx)
    db.commit()


# ---------------------------------------------------------------------------
# Invoices
# ---------------------------------------------------------------------------

@router.post("/invoices/", response_model=InvoiceResponse)
async def create_invoice(data: InvoiceCreate, db: db_dependency):
    if not db.query(models.Customer).filter(models.Customer.id == data.customer_id).first():
        raise HTTPException(status_code=404, detail=f'Customer {data.customer_id} not found')
    db_inv = models.Invoice(**data.model_dump())
    db.add(db_inv)
    db.commit()
    db.refresh(db_inv)
    return db_inv


@router.get("/invoices/", response_model=List[InvoiceResponse])
async def get_invoices(db: db_dependency, skip: int = 0, limit: int = 100):
    return db.query(models.Invoice).offset(skip).limit(limit).all()


@router.get("/invoices/{invoice_id}", response_model=InvoiceResponse)
async def get_invoice(invoice_id: int, db: db_dependency):
    inv = db.query(models.Invoice).filter(models.Invoice.id == invoice_id).first()
    if not inv:
        raise HTTPException(status_code=404, detail='Invoice not found')
    return inv


@router.delete("/invoices/{invoice_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_invoice(invoice_id: int, db: db_dependency):
    inv = db.query(models.Invoice).filter(models.Invoice.id == invoice_id).first()
    if not inv:
        raise HTTPException(status_code=404, detail='Invoice not found')
    db.delete(inv)
    db.commit()


# ---------------------------------------------------------------------------
# Services / Roles  (kept for existing data)
# ---------------------------------------------------------------------------

@router.post("/services/")
async def create_service(svc: service, db: db_dependency):
    if db.query(models.Service).filter(models.Service.name == svc.name).first():
        return {"error": f"Service '{svc.name}' already exists"}
    db_svc = models.Service(name=svc.name, description=svc.description, DBName=svc.DBName)
    db.add(db_svc)
    db.commit()
    db.refresh(db_svc)
    return {"status": "created", "service": db_svc}


@router.get("/services/")
async def get_services(db: db_dependency, skip: int = 0, limit: int = 100):
    return db.query(models.Service).offset(skip).limit(limit).all()


@router.delete("/services/{service_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_service(service_id: int, db: db_dependency):
    svc = db.query(models.Service).filter(models.Service.id == service_id).first()
    if not svc:
        raise HTTPException(status_code=404, detail='Service not found')
    db.delete(svc)
    db.commit()


@router.post("/roles/")
async def create_role(r: Role, db: db_dependency):
    if db.query(models.Role).filter(models.Role.role == r.role).first():
        return {"error": f"Role '{r.role}' already exists"}
    db_role = models.Role(role=r.role, description=r.desc)
    db.add(db_role)
    db.commit()
    db.refresh(db_role)
    return {"status": "created", "role": db_role}


@router.get("/roles/")
async def get_roles(db: db_dependency, skip: int = 0, limit: int = 100):
    return db.query(models.Role).offset(skip).limit(limit).all()


@router.delete("/roles/{role_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_role(role_id: int, db: db_dependency):
    role = db.query(models.Role).filter(models.Role.id == role_id).first()
    if not role:
        raise HTTPException(status_code=404, detail='Role not found')
    db.delete(role)
    db.commit()


# ---------------------------------------------------------------------------
# Registrations  (legacy)
# ---------------------------------------------------------------------------

@router.post("/registrations/")
async def create_registration(registration: RegistrationBase, db: db_dependency):
    _fields = {
        "firstname", "lastname", "username", "useremail",
        "clientname", "servicename", "clientemail",
        "contactphoneno", "address", "demodate", "createdate",
    }
    db_reg = models.Registraion(**registration.model_dump(include=_fields))
    db.add(db_reg)
    db.commit()
    db.refresh(db_reg)
    return {"status": "created", "registration": db_reg}


@router.get("/registrations/")
async def get_registrations(db: db_dependency, skip: int = 0, limit: int = 100):
    return db.query(models.Registraion).offset(skip).limit(limit).all()


@router.delete("/registrations/{registration_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_registration(registration_id: int, db: db_dependency):
    reg = db.query(models.Registraion).filter(models.Registraion.id == registration_id).first()
    if not reg:
        raise HTTPException(status_code=404, detail='Registration not found')
    db.delete(reg)
    db.commit()


# ---------------------------------------------------------------------------
# Products
# ---------------------------------------------------------------------------

@router.post("/products/", response_model=ProductResponse, status_code=status.HTTP_201_CREATED)
async def create_product(data: ProductCreate, db: db_dependency):
    if data.sku and db.query(models.Product).filter(models.Product.sku == data.sku).first():
        raise HTTPException(status_code=400, detail=f"SKU '{data.sku}' already exists")
    db_product = models.Product(**data.model_dump())
    db.add(db_product)
    db.commit()
    db.refresh(db_product)
    return db_product


@router.get("/products/", response_model=List[ProductResponse])
async def get_products(db: db_dependency, skip: int = 0, limit: int = 100):
    return db.query(models.Product).offset(skip).limit(limit).all()


@router.get("/products/{product_id}", response_model=ProductResponse)
async def get_product(product_id: int, db: db_dependency):
    product = db.query(models.Product).filter(models.Product.id == product_id).first()
    if not product:
        raise HTTPException(status_code=404, detail='Product not found')
    return product


@router.delete("/products/{product_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_product(product_id: int, db: db_dependency):
    product = db.query(models.Product).filter(models.Product.id == product_id).first()
    if not product:
        raise HTTPException(status_code=404, detail='Product not found')
    db.delete(product)
    db.commit()

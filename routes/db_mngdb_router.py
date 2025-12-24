from bson import objectid
from bson import ObjectId
from fastapi import status, File, UploadFile,HTTPException
from typing import List
from datetime import datetime
from models_mgdb.users import user
from models_mgdb.db import users as users_coll, services as services_coll, roles as roles_coll, transactions as transactions_coll, registrations as registrations_coll
from schemas_mgdb.serializeobjects import serializeDict, serializeList
from schemas_pgrs.schema import (
    service as ServiceSchema,
    Role as RoleSchema,
    TransactonBase as TransactionSchema,
    RegistrationBase as RegistrationSchema,
    CustomerBase,
    DeviceBase,
    CustomerResponse,
    DeviceResponse,
)
from utils.utils import getResponse, riseHttpExceptionIfNotFound
from helpers.save_picture import save_picture
from services import user_service as service
from fastapi import APIRouter
from dotenv import load_dotenv
from models_mgdb.db import db
from services.user_service import Hasher
import asyncio
import os
router = APIRouter(
    prefix = '/api/mongo',
    tags = ['MongoDB APIs']
)
base = '/Customers/'
UploadImage = f'{base}image-upload/'

_notFoundMessage = "Could not find user with the given Id."

# Map route collection names to Motor collection objects
_coll_map = {
    'users': users_coll,
    'services': services_coll,
    'roles': roles_coll,
    'transactions': transactions_coll,
    'registrations': registrations_coll,
}
# Add customers and devices to collection map if present in db module
try:
    _coll_map['customers'] = customers_coll
except NameError:
    # customers_coll may not be imported into this module; import from models_mgdb.db
    from models_mgdb.db import customers as customers_coll
    _coll_map['customers'] = customers_coll

try:
    _coll_map['devices'] = devices_coll
except NameError:
    from models_mgdb.db import devices as devices_coll
    _coll_map['devices'] = devices_coll


@router.get(base)
async def getallusers():
    return await service.getAllUser()


# New endpoints matching Postgres CRUD endpoints but for MongoDB
@router.get('/users/')
async def mongo_get_users():
    users = []
    async for u in users_coll.find():
        users.append(serializeDict(u))
    return users


@router.post('/users/')
async def mongo_create_user(data: user):
    # Hash password and avoid storing raw password in MongoDB mirrors
    plain_pwd = data.password
    hashed = Hasher.get_password_hash(plain_pwd)
    payload = data.model_dump(exclude={"password"})
    payload["passwordHash"] = hashed
    res = await users_coll.insert_one(payload)
    created = await users_coll.find_one({'_id': res.inserted_id})
    return serializeDict(created)


@router.get('/services/')
async def mongo_get_services():
    items = []
    async for s in services_coll.find():
        items.append(serializeDict(s))
    return items


@router.get('/customers/', response_model=List[CustomerResponse])
async def mongo_get_customers():
    items = []
    async for c in customers_coll.find():
        items.append(serializeDict(c))
    return items


@router.post('/customers/', response_model=CustomerResponse)
async def mongo_create_customer(data: CustomerBase):
    payload = data.model_dump(exclude_none=True)
    payload.setdefault('createdAt', datetime.utcnow())
    res = await customers_coll.insert_one(payload)
    created = await customers_coll.find_one({'_id': res.inserted_id})
    return serializeDict(created)


@router.get('/devices/', response_model=List[DeviceResponse])
async def mongo_get_devices():
    items = []
    async for d in devices_coll.find():
        items.append(serializeDict(d))
    return items


@router.post('/devices/', response_model=DeviceResponse)
async def mongo_create_device(data: DeviceBase):
    payload = data.model_dump(exclude_none=True)
    payload.setdefault('createdAt', datetime.utcnow())
    res = await devices_coll.insert_one(payload)
    created = await devices_coll.find_one({'_id': res.inserted_id})
    return serializeDict(created)


@router.post('/services/')
async def mongo_create_service(data: ServiceSchema):
    payload = data.model_dump()
    res = await services_coll.insert_one(payload)
    created = await services_coll.find_one({'_id': res.inserted_id})
    return serializeDict(created)


@router.get('/roles/')
async def mongo_get_roles():
    items = []
    async for r in roles_coll.find():
        items.append(serializeDict(r))
    return items


@router.post('/roles/')
async def mongo_create_role(data: RoleSchema):
    payload = data.model_dump()
    res = await roles_coll.insert_one(payload)
    created = await roles_coll.find_one({'_id': res.inserted_id})
    return serializeDict(created)


@router.get('/transactions/')
async def mongo_get_transactions():
    items = []
    async for t in transactions_coll.find():
        items.append(serializeDict(t))
    return items


@router.post('/transactions/')
async def mongo_create_transaction(data: TransactionSchema):
    payload = data.model_dump()
    res = await transactions_coll.insert_one(payload)
    created = await transactions_coll.find_one({'_id': res.inserted_id})
    return serializeDict(created)


@router.get('/registrations/')
async def mongo_get_registrations():
    items = []
    async for r in registrations_coll.find():
        items.append(serializeDict(r))
    return items


@router.post('/registrations/')
async def mongo_create_registration(data: RegistrationSchema):
    payload = data.model_dump()
    res = await registrations_coll.insert_one(payload)
    created = await registrations_coll.find_one({'_id': res.inserted_id})
    return serializeDict(created)


@router.get('/customers/{id}', response_model=CustomerResponse)
async def mongo_get_customer_by_id(id: str):
    try:
        obj_id = ObjectId(id)
    except Exception:
        raise HTTPException(status_code=400, detail='Invalid id')
    doc = await customers_coll.find_one({'_id': obj_id})
    await riseHttpExceptionIfNotFound(doc, message=f"Customer {id} not found")
    return serializeDict(doc)


@router.get('/devices/{id}', response_model=DeviceResponse)
async def mongo_get_device_by_id(id: str):
    try:
        obj_id = ObjectId(id)
    except Exception:
        raise HTTPException(status_code=400, detail='Invalid id')
    doc = await devices_coll.find_one({'_id': obj_id})
    await riseHttpExceptionIfNotFound(doc, message=f"Device {id} not found")
    return serializeDict(doc)


@router.put('/customers/{id}')
async def mongo_update_customer(id: str, data: dict):
    try:
        obj_id = ObjectId(id)
    except Exception:
        raise HTTPException(status_code=400, detail='Invalid id')
    await customers_coll.update_one({'_id': obj_id}, {'$set': data})
    return getResponse(True)


@router.put('/devices/{id}')
async def mongo_update_device(id: str, data: dict):
    try:
        obj_id = ObjectId(id)
    except Exception:
        raise HTTPException(status_code=400, detail='Invalid id')
    await devices_coll.update_one({'_id': obj_id}, {'$set': data})
    return getResponse(True)


@router.delete('/customers/{id}')
async def mongo_delete_customer(id: str):
    try:
        obj_id = ObjectId(id)
    except Exception:
        raise HTTPException(status_code=400, detail='Invalid id')
    await customers_coll.delete_one({'_id': obj_id})
    return getResponse(True)


@router.delete('/devices/{id}')
async def mongo_delete_device(id: str):
    try:
        obj_id = ObjectId(id)
    except Exception:
        raise HTTPException(status_code=400, detail='Invalid id')
    await devices_coll.delete_one({'_id': obj_id})
    return getResponse(True)


@router.get(base+'{id}')
async def getuserbyid(id):
    return await resultVerification(id)

@router.post(base)
async def insertnewuser(data: user):
    data.password = Hasher.get_password_hash(data.password)

    return await service.InsertUser(data)


@router.put(base+'{id}', status_code=status.HTTP_204_NO_CONTENT)
async def updateUser(id, data: user):
    await resultVerification(id)
    done : bool = await service.updateUser(id,data);
    return getResponse(done, errorMessage="An error occurred while editing the user information.")


@router.delete(base+'{id}', status_code=status.HTTP_204_NO_CONTENT)
async def deleteUser(id):
    await resultVerification(id)
    done : bool = await service.deleteUser(id);
    return getResponse(done, errorMessage="There was an error.")   


@router.post(UploadImage+'{id}', status_code=status.HTTP_204_NO_CONTENT)
async def uploadUserImage(id: str, file: UploadFile = File(...)):
    result = await resultVerification(id)
    imageUrl = save_picture(file=file, folderName='users', fileName=result['name'])
    done = await service.savePicture(id, imageUrl)
    return getResponse(done, errorMessage="An error occurred while saving user image.")


# Generic image upload endpoint for any supported collection
@router.post('/{collection}/{id}/image', status_code=status.HTTP_204_NO_CONTENT)
async def upload_collection_image(collection: str, id: str, file: UploadFile = File(...)):
    """Upload an image/file and attach `imageUrl` to the document in the given collection.

    Supported collections: `users`, `services`, `roles`, `transactions`, `registrations`.
    """
    collection = collection.lower()
    coll = _coll_map.get(collection)
    if coll is None:
        raise HTTPException(status_code=404, detail=f"Collection '{collection}' not found")

    # validate id and document exists
    try:
        obj_id = ObjectId(id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid document id")

    doc = await coll.find_one({'_id': obj_id})
    if not doc:
        raise HTTPException(status_code=404, detail=f"Document {id} not found in {collection}")

    # save image using existing helper (saves to `static` and returns URL/path)
    imageUrl = save_picture(file=file, folderName=collection, fileName=str(id))

    # update document with imageUrl
    await coll.update_one({'_id': obj_id}, {'$set': {'imageUrl': imageUrl}})

    return getResponse(True, errorMessage=f"An error occurred while saving {collection} image.")


# Convenience endpoints for each collection (call generic handler)
@router.post('/users/{id}/image', status_code=status.HTTP_204_NO_CONTENT)
async def upload_users_image(id: str, file: UploadFile = File(...)):
    return await upload_collection_image('users', id, file)


@router.post('/services/{id}/image', status_code=status.HTTP_204_NO_CONTENT)
async def upload_services_image(id: str, file: UploadFile = File(...)):
    return await upload_collection_image('services', id, file)


@router.post('/roles/{id}/image', status_code=status.HTTP_204_NO_CONTENT)
async def upload_roles_image(id: str, file: UploadFile = File(...)):
    return await upload_collection_image('roles', id, file)


@router.post('/transactions/{id}/image', status_code=status.HTTP_204_NO_CONTENT)
async def upload_transactions_image(id: str, file: UploadFile = File(...)):
    return await upload_collection_image('transactions', id, file)


@router.post('/registrations/{id}/image', status_code=status.HTTP_204_NO_CONTENT)
async def upload_registrations_image(id: str, file: UploadFile = File(...)):
    return await upload_collection_image('registrations', id, file)



# Helpers

async def resultVerification(id: objectid) -> dict:
    result = await service.getById(id)
    await riseHttpExceptionIfNotFound(result, message=_notFoundMessage)
    return result
#Jason file upload
@router.post("/upload_json__mongodb/")
async def uploadjsondata(file: UploadFile):
    import json
    file.file.seek(0, 2)
    file_size = file.file.tell()

    # move the cursor back to the beginning
    await file.seek(0)

    if file_size > 2 * 1024 * 1024:
        # more than 2 MB
        raise HTTPException(status_code=200, detail="File too large")

    # check the content type (MIME type)
    content_type = file.content_type
    if content_type in ["image/jpeg", "image/png", "image/gif"]:
        raise HTTPException(status_code=400, detail="Invalid file type")
    #from pymongo import MongoClient 
    # Making Connection

    load_dotenv()  # take environment variables from .env.
    if file.filename.endswith(".json"):
        try:
            tablename  =get_file_name(file.filename)
            print(tablename)
            print(file.filename)
            json_data = json.load(file.file)
            print(json_data)
            print(db.name)
            # Use Motor async insert_many for the async DB
            Collection = db[tablename]
            if isinstance(json_data, list):
                await Collection.insert_many(json_data)
            else:
                await Collection.insert_one(json_data)
            # if isinstance(json_data, list):
            #     db.Collection.insert_many(json_data) 
            # else:
            #     Collection.insert_one(json_data)
            #         #insert_file_data(json_data=data,Collection=Collection)

        except:
            print("Sorry,  load error has occurred!")
   
    #DB=os.getenv("MONGODB_URL")
    #myclient = MongoClient(MONGODB_URL) 
# database 
# Created or Switched to collection 
# names: GeeksForGeeks
    
# Loading or Opening the json file
#Using the loads() method, you can see that the parsed_json variable now has a valid dictionary. 
# #From this dictionary, you can access the keys and values in it.



# Inserting the loaded data in the Collection
# if JSON contains data more than one entry
# insert_many is used else insert_one is used
def insert_file_data(json_data,Collection):
    # helper kept for sync-style callers; perform operations synchronously
    if isinstance(json_data, list):
        Collection.insert_many(json_data)
    else:
        Collection.insert_one(json_data)

def get_file_name(file_path):
        file_path_components = file_path.split('/')
        file_path_components = file_path.split('\\')

        file_name_and_extension = file_path_components[-1].rsplit('.', 1)
        return file_name_and_extension[0]
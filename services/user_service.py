from bson import ObjectId
from models_mgdb.db import db
from models_mgdb.users import user
from schemas_mgdb.serializeobjects import serializeDict, serializeList
from passlib.context import CryptContext
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
from fastapi import status, File, UploadFile,HTTPException
from typing import Union
from fastapi.encoders import jsonable_encoder
import json
from dotenv import load_dotenv
import os
import asyncio
import motor.motor_asyncio
from bson import json_util

#import pymango
class Hasher():
    @staticmethod
    def verify_password(plain_password, hashed_password):
        return pwd_context.verify(plain_password, hashed_password)

    @staticmethod
    def get_password_hash(password):
        return pwd_context.hash(password)
    
async def getAllUser() -> list:
    users = []
    async for user in db.users.find():
        users.append(serializeDict(user))
    return users    #return await db.get_collection("user")

# async def fetch_all_todos():
#     todos = []
#     cursor = await Collection.find({})
#     async for document in cursor:
#         todos.append(ToDo(**document))
#     return todos

async def getById(id: str):
    #return serializeDict(db.users.find_one({"_id": ObjectId(id)}))    
    if (
        user := await db.users.find_one({"_id": ObjectId(id)})
    ) is not None:
        return serializeDict(user)

    raise HTTPException(status_code=404, detail=f"Student {id} not found")

async def InsertUser(data: user):
    #result = db.users.insert_one(dict(data))
    #return serializeDict(db.users.find_one({"_id": ObjectId(result.inserted_id)}))
        
    new_user = await db.users.insert_one(
        data.model_dump(by_alias=True, exclude=["id"])
                     )
    created_user = await db.users.find_one(
        {"_id": new_user.inserted_id}
    )   
    return    (json_util.dumps(created_user))


#    return json.loads(json_util.dumps(data))
#json.dumps(my_obj, default=str)

async def updateUser(id, data: user) -> bool:
    db.users.find_one_and_update({"_id": ObjectId(id)}, {"$set": dict(data)})
    return True

async def savePicture(id, imageUrl: str) -> bool:
    db.users.find_one_and_update({"_id": ObjectId(id)}, {"$set": { "imageUrl": imageUrl }})
    return True


async def deleteUser(id) -> bool:
    db.users.find_one_and_delete({"_id": ObjectId(id)})
    return True



async def uploadjsondata(jsfile: Union[UploadFile, str], db: str, mycollection: str):
    """Upload JSON data into the given MongoDB database and collection.

    `jsfile` may be a path (str) or a FastAPI `UploadFile`/file-like object.
    """
    load_dotenv()
    MONGODB_URL = os.getenv("MONGODB_URL")
    if not MONGODB_URL:
        raise RuntimeError("MONGODB_URL not set in environment")

    client = motor.motor_asyncio.AsyncIOMotorClient(MONGODB_URL)
    DB = client[db]
    Collection = DB[mycollection]

    # load JSON from upload or local path
    if isinstance(jsfile, str):
        # path to file on disk
        with open(jsfile, "r", encoding="utf-8") as f:
            file_data = json.load(f)
    else:
        # UploadFile or file-like object
        # if it's a FastAPI UploadFile, read bytes asynchronously
        try:
            # UploadFile has .read method which is async
            content = await jsfile.read()
            file_data = json.loads(content.decode("utf-8"))
        except AttributeError:
            # fallback to file-like object with sync .read()
            content = jsfile.file.read()
            if isinstance(content, bytes):
                file_data = json.loads(content.decode("utf-8"))
            else:
                file_data = json.loads(content)

    # insert into collection
    if isinstance(file_data, list):
        await Collection.insert_many(file_data)
    else:
        await Collection.insert_one(file_data)

def load_json_file(file_name: str):
    # Loading or Opening the json file (sync helper)
    with open(file_name, "r", encoding="utf-8") as file:
        return json.load(file)

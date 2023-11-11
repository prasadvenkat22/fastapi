from bson import ObjectId
from config.db_mgdb import db
from models_mgdb.users import user
from schemas_mgdb.serializeobjects import serializeDict, serializeList
from passlib.context import CryptContext
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
from fastapi import status, File, UploadFile,HTTPException
from fastapi.encoders import jsonable_encoder
import json
from dotenv import load_dotenv
import os
import asyncio
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



def uploadjsondata(jsfile:File,db:str, mycollection:str):
    
    load_dotenv()  # take environment variables from .env.
    from pymongo import MongoClient as pm
    mongourl=os.getenv(mongourl)
    myclient = pm.MongoClient(mongourl) 
    # database 
    DB = myclient[db]
    # Created or Switched to collection 
    number = 10
    Collection = DB[mycollection]
    file_data = load_json_file(jsfile)
  
    if number > 0:
        print('Positive number')
    else:
        print('Negative number')
# Inserting the loaded data in the Collection
# if JSON contains data more than one entry
# insert_many is used else insert_one is used
    if isinstance(file_data, list):
        Collection.insert_many(file_data)  
    else:
        Collection.insert_one(file_data)
	#Collection.insert_one(file_data)
# Inserting the loaded data in the Collection
# if JSON contains data more than one entry
# insert_many is used else insert_one is used

def load_json_file(file_name:str):
# Loading or Opening the json file
# or use   data = json.load(f)
    with open(file_name, 'r') as file:
	    return json.load(file)

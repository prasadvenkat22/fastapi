from bson import objectid
from fastapi import status, File, UploadFile,HTTPException
from models_mgdb.users import user
from utils.utils import getResponse, riseHttpExceptionIfNotFound
from helpers.save_picture import save_picture
from services import user_service as service
from fastapi import APIRouter
from dotenv import load_dotenv
from config.db_mgdb import db
from services.user_service import Hasher
import asyncio
import os
router = APIRouter(
    prefix = '/CRUD',
    tags = ['MongoDB APIs']
)
base = '/Customers/'
UploadImage = f'{base}image-upload/'

_notFoundMessage = "Could not find user with the given Id."


@router.get(base)
async def getallusers():
    return await service.getAllUser()


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
            Collection = db[tablename]
            Collection.insert_many(json_data) 
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
    
    if isinstance(json_data, list):
        Collection.insert_many(json_data) 
    else:
	    Collection.insert_one(json_data)

def get_file_name(file_path):
        file_path_components = file_path.split('/')
        file_path_components = file_path.split('\\')

        file_name_and_extension = file_path_components[-1].rsplit('.', 1)
        return file_name_and_extension[0]
from dotenv import load_dotenv
import os
#from pymango import MongoClient

import motor.motor_asyncio

load_dotenv()  # take environment variables from .env.

DB=os.getenv("DB_NAME")
# client = MongoClient(os.getenv("MONGODB_URL"))
# db = client.admin
# collection_name = db["users"]
#print(DB)
# app = FastAPI()
#client = MongoClient(os.getenv("MONGODB_URL"))
#db = client.users
#client = MongoClient(os.getenv("MONGODB_URL"))

client = motor.motor_asyncio.AsyncIOMotorClient(os.environ["MONGODB_URL"])
db = client.get_database("users")
#database = client["mydatabase"]
# Send a ping to confirm a successful connection
# try:
#     client.admin.command('ping')
#     print("Pinged your deployment. You have successfully connected to MongoDB!")
# except Exception as e:
#     print(e)
#student_collection = db.get_collection("students")
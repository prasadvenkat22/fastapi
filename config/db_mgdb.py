from dotenv import load_dotenv
import os
#from pymango import MongoClient

import motor.motor_asyncio

load_dotenv()  # take environment variables from .env.
DB_NAME = os.getenv("DB_NAME", "fastapi_db")
MONGODB_URL = os.getenv("MONGODB_URL", "mongodb://localhost:27017")

# Create async Motor client and select database from env
client = motor.motor_asyncio.AsyncIOMotorClient(MONGODB_URL)
db = client.get_database(DB_NAME)

# Expose collections matching Postgres tables so other modules can import
# and use either `db.<collection>` or the named collection variables below.
users = db.get_collection("users")
services = db.get_collection("services")
roles = db.get_collection("roles")
transactions = db.get_collection("transactions")
registrations = db.get_collection("registrations")
#database = client["mydatabase"]
# Send a ping to confirm a successful connection
# try:
#     client.admin.command('ping')
#     print("Pinged your deployment. You have successfully connected to MongoDB!")
# except Exception as e:
#     print(e)
#student_collection = db.get_collection("students")
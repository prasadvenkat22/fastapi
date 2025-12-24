from dotenv import load_dotenv
import os
import motor.motor_asyncio

load_dotenv()  # take environment variables from .env.

DB_NAME = os.getenv("DB_NAME", "fastapi_db")
MONGODB_URL = os.getenv("MONGODB_URL", "mongodb://localhost:27017")

# Create async Motor client and select database from env
client = motor.motor_asyncio.AsyncIOMotorClient(MONGODB_URL)
db = client.get_database(DB_NAME)

# Expose collections matching Postgres tables so other modules can import
users = db.get_collection("users")
services = db.get_collection("services")
roles = db.get_collection("roles")
transactions = db.get_collection("transactions")
registrations = db.get_collection("registrations")

# Additional collections for proposed schema
customers = db.get_collection("customers")
devices = db.get_collection("devices")
audit_logs = db.get_collection("audit_logs")
attachments = db.get_collection("attachments")
invoices = db.get_collection("invoices")

# backward-compatible alias for code that imports `db` from config
# (other modules should import from `models_mgdb.db`)

from dotenv import load_dotenv
import os
import motor.motor_asyncio

load_dotenv()  # take environment variables from .env.

DB_NAME = os.getenv("DB_NAME", "fastapi_db")
MONGODB_URL = os.getenv("MONGODB_URL", "mongodb://localhost:27017")

# Do not create the Motor client at import-time to avoid capturing
# an event loop that may later be closed (causes "Event loop is closed").
# Initialize the client during application startup via `init_db()`.
client = None
db = None


async def init_db(mongo_url: str = None, db_name: str = None):
	"""Initialize Motor client and database. Call from FastAPI startup."""
	global client, db
	url = mongo_url or MONGODB_URL
	name = db_name or DB_NAME
	client = motor.motor_asyncio.AsyncIOMotorClient(url)
	db = client.get_database(name)


def get_collection(name: str):
	"""Return a Motor collection by name. Raises if DB not initialized."""
	if db is None:
		raise RuntimeError("MongoDB client not initialized. Call init_db() on startup.")
	return db.get_collection(name)


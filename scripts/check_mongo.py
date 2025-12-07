import os
from dotenv import load_dotenv

load_dotenv()
MONGO_URL = os.getenv('MONGODB_URL')
DB_NAME = os.getenv('DB_NAME') or os.getenv('DB_NAME', 'fastapi_db')
print('MONGODB_URL=', MONGO_URL)
print('DB_NAME=', DB_NAME)

try:
    from pymongo import MongoClient
except Exception as e:
    print('pymongo not installed or import failed:', e)
    raise SystemExit(1)

try:
    client = MongoClient(MONGO_URL)
    db = client[DB_NAME.strip(' "')]
    print('Connected to Mongo, listing collections:')
    cols = db.list_collection_names()
    for c in cols:
        print(' -', c)
    if not cols:
        print('(no collections found)')
except Exception as e:
    print('Error connecting to MongoDB:', e)
    raise SystemExit(2)

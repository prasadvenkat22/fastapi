import os
from dotenv import load_dotenv
load_dotenv()
from pymongo import MongoClient

MONGO_URL = os.getenv('MONGODB_URL')
DB_NAME = os.getenv('DB_NAME')
if not MONGO_URL:
    raise SystemExit('MONGODB_URL not set in env')
if not DB_NAME:
    raise SystemExit('DB_NAME not set in env')

client = MongoClient(MONGO_URL)
db = client[DB_NAME.strip(' "')]

samples = {
    'users': {'name':'seed_user','email':'seed@example.com'},
    'services': {'name':'seed_service','description':'seed'},
    'roles': {'role':'user','description':'seed role'},
    'transactions': {'amount':0.0,'category':'seed','description':'seed','is_income':True,'date':'2025-12-07'},
    'registrations': {'firstname':'Seed','lastname':'User','username':'seed','useremail':'seed@example.com'}
}

for coll, doc in samples.items():
    c = db[coll]
    res = c.insert_one(doc)
    print(f'Inserted into {coll}:', res.inserted_id)

print('Done')

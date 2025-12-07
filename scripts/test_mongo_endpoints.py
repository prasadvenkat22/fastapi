import os
import requests
from datetime import datetime

BASE = os.getenv('BASE_URL','http://localhost:8000')

endpoints = {
    'get_users': f"{BASE}/api/mongo/users/",
    'post_user': f"{BASE}/api/mongo/users/",
    'get_services': f"{BASE}/api/mongo/services/",
    'post_service': f"{BASE}/api/mongo/services/",
}

print('Base URL:', BASE)

def safe_get(url):
    try:
        r = requests.get(url, timeout=5)
        print(f'GET {url} ->', r.status_code)
        print(r.text)
    except Exception as e:
        print(f'GET {url} -> ERROR:', e)

def safe_post(url, payload):
    try:
        r = requests.post(url, json=payload, timeout=5)
        print(f'POST {url} ->', r.status_code)
        print(r.text)
    except Exception as e:
        print(f'POST {url} -> ERROR:', e)

# Test GET users
safe_get(endpoints['get_users'])

# Test POST service
svc_payload = {
    'name': 'seed_service_test',
    'description': 'created by test script',
    'DBName': 'postgres',
    'createdate': datetime.utcnow().isoformat()
}
safe_post(endpoints['post_service'], svc_payload)

# Test GET services
safe_get(endpoints['get_services'])

# Test POST user
user_payload = {
    'name': 'seed_user_test',
    'email': 'seed.user@test.local',
    'password': 'pass123',
    'tenantdb': 'TENANT',
    'application': 'testapp',
    'role': 'user',
    'status': False,
    'date': datetime.utcnow().isoformat()
}
safe_post(endpoints['post_user'], user_payload)

# Final GET users to see new user
safe_get(endpoints['get_users'])

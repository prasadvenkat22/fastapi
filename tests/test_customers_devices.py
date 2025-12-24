import pytest
import sys, os
# Ensure repo root is on sys.path so tests can import application modules
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from fastapi.testclient import TestClient
from main import app


def test_customers_crud():
    # create customer (create TestClient inside test so startup runs after fixtures)
    with TestClient(app) as client:
        res = client.post('/api/mongo/customers/', json={"name": "TestCo", "createdAt": None})
        assert res.status_code in (200, 201)
        data = res.json()
        assert 'id' in data or '_id' in data or data.get('name') == 'TestCo'


def test_devices_crud():
    # create device
    with TestClient(app) as client:
        res = client.post('/api/mongo/devices/', json={"serialNumber": "SN12345", "createdAt": None})
        assert res.status_code in (200, 201)
        data = res.json()
        assert 'id' in data or '_id' in data or data.get('serialNumber') == 'SN12345'

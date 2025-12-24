import pytest
from fastapi.testclient import TestClient
from main import app

client = TestClient(app)

@pytest.mark.asyncio
def test_customers_crud():
    # create customer
    res = client.post('/api/mongo/customers/', json={"name":"TestCo","createdAt":None})
    assert res.status_code in (200,201)
    data = res.json()
    assert 'id' in data or '_id' in data or data.get('name') == 'TestCo'

@pytest.mark.asyncio
def test_devices_crud():
    # create device
    res = client.post('/api/mongo/devices/', json={"serialNumber":"SN12345","createdAt":None})
    assert res.status_code in (200,201)
    data = res.json()
    assert 'id' in data or '_id' in data or data.get('serialNumber') == 'SN12345'

import pytest
from unittest.mock import patch
import mongomock

@pytest.fixture(autouse=True)
def mock_mongo_db():
    with patch('models_mgdb.db.init_db', return_value=None) as mock_init_db:
        with patch('models_mgdb.db.get_collection') as mock_get_collection:
            mock_get_collection.side_effect = mongomock.MongoClient().db.get_collection
            yield


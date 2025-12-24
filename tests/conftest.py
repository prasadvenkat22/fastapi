import os
import socket
import time
import pytest


def _parse_mongodb_host_port(url: str):
    # crude parser for mongodb://host:port or mongodb://user:pass@host:port
    if not url:
        return 'localhost', 27017
    url = url.split('://', 1)[-1]
    # strip any trailing path
    hostport = url.split('/', 1)[0]
    # if user:pass@host:port
    if '@' in hostport:
        hostport = hostport.split('@', 1)[1]
    if ':' in hostport:
        host, port = hostport.split(':', 1)
        try:
            return host, int(port)
        except ValueError:
            return host, 27017
    return hostport, 27017


@pytest.fixture(scope='session', autouse=True)
def wait_for_mongo():
    """Wait for MongoDB to be reachable at MONGODB_URL (or localhost:27017).

    If Mongo is not reachable within timeout, tests will fail early.
    """
    url = os.getenv('MONGODB_URL', 'mongodb://localhost:27017')
    host, port = _parse_mongodb_host_port(url)
    timeout = 30
    deadline = time.time() + timeout
    last_exc = None
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=2):
                # success
                return
        except Exception as e:
            last_exc = e
            time.sleep(0.5)
    pytest.exit(f"MongoDB at {host}:{port} not reachable after {timeout}s: {last_exc}")

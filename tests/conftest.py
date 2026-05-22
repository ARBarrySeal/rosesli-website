import os
import pytest

os.environ.setdefault("TESTING", "1")
os.environ.setdefault("DB_HOST", "localhost")
os.environ.setdefault("DB_NAME", "botdb")
os.environ.setdefault("DB_USER", "botuser")
os.environ.setdefault("DB_PASS", "password")
os.environ.setdefault("JWT_SECRET", "test-secret-key-for-pytest-only-32chars!!")
os.environ.setdefault("FLASK_SECRET_KEY", "test-flask-secret-for-pytest-only!!")
os.environ.setdefault("COMPANY_ID", "rosesli")
os.environ.setdefault("APP_URL", "http://localhost:8080")

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from main import app as flask_app

@pytest.fixture()
def app():
    flask_app.config["TESTING"] = True
    yield flask_app

@pytest.fixture()
def client(app):
    return app.test_client()

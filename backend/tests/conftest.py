import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_tmp = tempfile.mkdtemp(prefix="method-agree-tests-")
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp}/test.db"
os.environ.setdefault("REDIS_URL", "redis://localhost:6399/0")
os.environ.setdefault("JWT_SECRET", "test-secret")

from src.config.database import Base, Session, engine  # noqa: E402
from src import models  # noqa: F401,E402
from src.services import monitoring_service, run_service  # noqa: E402


class FakeQueue:
    def __init__(self):
        self.items = []

    def lpush(self, key, value):
        self.items.append((key, value))

    def brpop(self, keys, timeout=2):
        return None


@pytest.fixture()
def db():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    s = Session()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture(autouse=True)
def fake_queue(monkeypatch):
    queue = FakeQueue()
    monkeypatch.setattr(monitoring_service, "queue", queue)
    monkeypatch.setattr(run_service, "queue", queue)
    return queue


@pytest.fixture()
def user():
    from src.models import User
    return User(id=1, email="analyst@ciguang.demo", name="江光", role="analyst", password_hash="x")

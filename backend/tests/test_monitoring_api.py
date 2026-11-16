import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DATABASE_URL", f"sqlite:///{tempfile.mkdtemp(prefix='method-agree-api-')}/api.db")
os.environ.setdefault("REDIS_URL", "redis://localhost:6399/0")
os.environ.setdefault("JWT_SECRET", "test-secret")

from src.config.database import Base, Session, engine  # noqa: E402
from src import models  # noqa: F401,E402
from src.models import User  # noqa: E402
from src.services import monitoring_service, run_service  # noqa: E402
from tests.test_monitoring import _user, drain, make_baseline  # noqa: E402
from src.schemas.monitoring import MonitorInput  # noqa: E402


class FakeQueue:
    def lpush(self, key, value):
        pass

    def brpop(self, keys, timeout=2):
        return None

    def ping(self):
        return True


@pytest.fixture(scope="module")
def client():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    queue = FakeQueue()
    monitoring_service.queue = queue
    run_service.queue = queue
    import src.main as main
    from src.utils.auth import current_user, editor, admin
    from fastapi.testclient import TestClient

    db = Session()
    analyst = User(email="analyst@test.demo", name="江光", role="analyst", password_hash="x")
    viewer = User(email="viewer@test.demo", name="许澄", role="viewer", password_hash="x")
    db.add_all([analyst, viewer])
    db.commit()
    state = {"user": analyst}

    from fastapi import HTTPException

    def as_editor():
        if state["user"].role not in ("admin", "analyst"):
            raise HTTPException(403, "观察员无权修改测量研究")
        return state["user"]

    main.app.dependency_overrides[current_user] = lambda: state["user"]
    main.app.dependency_overrides[editor] = as_editor
    main.app.dependency_overrides[admin] = as_editor

    def as_user(role):
        state["user"] = analyst if role == "analyst" else viewer

    with TestClient(main.app) as c:
        c.as_user = as_user
        yield c
    main.app.dependency_overrides.clear()


_counter = {"n": 0}


def _setup_monitor():
    _counter["n"] += 1
    tag = f"-{_counter['n']}"
    db = Session()
    run = make_baseline(db, tag)
    create = monitoring_service.create_monitor(
        db, MonitorInput(name=f"API监测{tag}", baseline_run_id=run.id, consecutive_limit=3), _user()
    )
    monitor_id = create["id"]
    drain(db, db.get(models.MonitoringProfile, monitor_id))
    db.close()
    return run.id, monitor_id


def test_monitor_api_flow(client):
    run_id, monitor_id = _setup_monitor()
    listing = client.get("/monitors")
    assert listing.status_code == 200
    item = next(m for m in listing.json() if m["id"] == monitor_id)
    assert item["published"]["version"] == 1
    assert item["batch_count"] == 0

    batch = {
        "batch_key": "2026-11-10",
        "sampling_date": "2026-11-10",
        "source_file": "b.csv",
        "csv_text": "sample_code,reference_value,candidate_value\n" + "\n".join(
            f"A{i},{12 + i},{12 + i + 0.4}" for i in range(10)
        ) + "\n",
    }
    created = client.post(f"/monitors/{monitor_id}/batches", json=batch)
    assert created.status_code == 201
    assert created.json()["duplicate"] is False
    assert created.json()["pending"]["stale"] is True

    repeat = client.post(f"/monitors/{monitor_id}/batches", json=batch)
    assert repeat.status_code == 201
    assert repeat.json()["duplicate"] is True

    detail = client.get(f"/monitors/{monitor_id}").json()
    assert detail["pending"]["stale"] is True
    assert detail["published"]["version"] == 1  # previous version still shown

    db = Session()
    drain(db, db.get(models.MonitoringProfile, monitor_id))
    db.close()
    detail = client.get(f"/monitors/{monitor_id}").json()
    assert detail["pending"]["stale"] is False
    assert detail["published"]["version"] == 2
    assert detail["published"]["result"]["summary"]["batch_count"] == 1

    replay = client.post(f"/monitors/{monitor_id}/replay", json={"from_date": "2026-11-10"})
    assert replay.status_code == 202
    db = Session()
    drain(db, db.get(models.MonitoringProfile, monitor_id))
    db.close()
    history = client.get(f"/monitors/{monitor_id}/evaluations").json()
    assert [h["version"] for h in history][:3] == [3, 2, 1]

    export = client.get(f"/monitors/{monitor_id}/export")
    assert export.status_code == 200
    body = export.content.decode("utf-8-sig")
    assert "# monitor," in body
    assert "first_failure_date" in body
    assert "2026-11-10" in body


def test_viewer_cannot_ingest(client):
    _, monitor_id = _setup_monitor()
    client.as_user("viewer")
    response = client.post(
        f"/monitors/{monitor_id}/batches",
        json={"batch_key": "v", "sampling_date": "2026-11-11", "csv_text": "sample_code,reference_value,candidate_value\n" + "\n".join(f"A{i},{10 + i},{10 + i}" for i in range(8)) + "\n"},
    )
    assert response.status_code == 403
    client.as_user("analyst")


def test_monitor_requires_completed_baseline(client):
    db = Session()
    run = make_baseline(db, "-incomplete")
    run.status = "failed"
    db.commit()
    db.close()
    response = client.post("/monitors", json={"name": "未完成基线监测", "baseline_run_id": run.id})
    assert response.status_code == 409

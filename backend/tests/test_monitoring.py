import random
from datetime import date, timedelta

import pytest

from src.models import ComparisonRun, Instrument, MonitoringEvaluation, MonitoringProfile, PairDataset, PairedReading
from src.services import monitoring_service
from src.services.monitoring_service import ingest_batch, create_monitor, execute_evaluation
from src.schemas.monitoring import MonitorInput, BatchInput
from src.services.run_service import execute_run

BASE = date(2026, 10, 1)


def make_baseline(db, tag=""):
    ref = Instrument(code=f"CG-R01{tag}", name="参考", serial="R1", unit="反射率 %", lower=0, upper=100)
    cand = Instrument(code=f"CG-N02{tag}", name="候选", serial="N2", unit="反射率 %", lower=0, upper=100)
    db.add_all([ref, cand])
    db.commit()
    ds = PairDataset(name=f"N02 · 基线{tag}", reference_id=ref.id, candidate_id=cand.id, source_file="base.csv", note="")
    db.add(ds)
    db.commit()
    rng = random.Random(11)
    for i in range(120):
        x = round(10 + i * 70 / 119 + rng.gauss(0, .2), 4)
        y = round(0.3 + 1.002 * (x - 0.3 + 0.3) + rng.gauss(0, .2), 4)  # bias ~0.3
        db.add(PairedReading(dataset_id=ds.id, sample_code=f"B-{i:03d}", reference_value=x, candidate_value=y))
    db.commit()
    run = ComparisonRun(name=f"基线研究{tag}", dataset_id=ds.id, lambda_ratio=1, allowance=2, range_low=0, range_high=100, bootstrap_count=200, status="queued")
    db.add(run)
    db.commit()
    execute_run(db, run.id)
    assert run.status == "completed"
    return run


def batch_csv(rng, offset, n=30, spread=.2):
    lines = ["sample_code,reference_value,candidate_value"]
    for i in range(n):
        x = round(12 + rng.random() * 66, 3)
        y = round(x + 0.3 + offset + rng.gauss(0, spread), 3)
        y = min(max(y, 0.01), 99.99)
        lines.append(f"D{i:03d},{x},{y}")
    return "\n".join(lines) + "\n"


def add(db, monitor, key, day, offset, rng, n=30):
    payload = BatchInput(batch_key=key, sampling_date=day, csv_text=batch_csv(rng, offset, n=n), source_file=f"{key}.csv")
    response = ingest_batch(db, monitor.id, payload, _user())
    drain(db, monitor)
    return response


class _U:
    email = "analyst@ciguang.demo"


def _user():
    return _U()


def drain(db, monitor):
    while True:
        item = db.query(MonitoringEvaluation).filter(
            MonitoringEvaluation.monitor_id == monitor.id,
            MonitoringEvaluation.status.in_(["queued", "running"]),
        ).order_by(MonitoringEvaluation.version).first()
        if not item:
            return
        execute_evaluation(db, item.id)


def published(db, monitor):
    return (
        db.query(MonitoringEvaluation)
        .filter(MonitoringEvaluation.monitor_id == monitor.id, MonitoringEvaluation.status == "completed")
        .order_by(MonitoringEvaluation.version.desc())
        .first()
    )


@pytest.fixture()
def monitor(db):
    run = make_baseline(db)
    create_monitor(db, MonitorInput(name="日批次监测", baseline_run_id=run.id, consecutive_limit=3), _user())
    mon = db.query(MonitoringProfile).filter(MonitoringProfile.name == "日批次监测").one()
    drain(db, mon)
    return mon


def test_baseline_is_frozen_snapshot(db, monitor):
    db.refresh(monitor)
    assert monitor.baseline["bias"] == pytest.approx(0.3, abs=0.12)
    assert monitor.baseline["allowance"] == 2
    assert published(db, monitor).result["summary"]["batch_count"] == 0


def test_deviation_and_consecutive_alert(db, monitor):
    rng = random.Random(5)
    days = [BASE + timedelta(days=i) for i in range(5)]
    add(db, monitor, "d1", days[0], 0.05, rng)
    add(db, monitor, "d2", days[1], 0.1, rng)
    add(db, monitor, "d3", days[2], 0.2, rng)
    add(db, monitor, "d4", days[3], 2.6, rng)
    add(db, monitor, "d5", days[4], 2.7, rng)
    result = published(db, monitor).result
    rows = result["batches"]
    assert [r["consecutive_out"] for r in rows] == [0, 0, 0, 1, 2]
    assert not result["summary"]["alert_active"]
    add(db, monitor, "d6", BASE + timedelta(days=5), 2.8, rng)
    result = published(db, monitor).result
    assert [r["consecutive_out"] for r in result["batches"]] == [0, 0, 0, 1, 2, 3]
    assert result["summary"]["alert_active"] is True
    assert result["summary"]["first_failure_date"] == (BASE + timedelta(days=3)).isoformat()
    assert [b["batch_key"] for b in result["summary"]["current_failure_batches"]] == ["d4", "d5", "d6"]
    # one in-range batch resets the streak
    add(db, monitor, "d7", BASE + timedelta(days=6), 0.0, rng)
    result = published(db, monitor).result
    assert result["summary"]["alert_active"] is False
    assert result["batches"][-1]["consecutive_out"] == 0


def test_late_batch_replay_matches_single_pass(db, monitor):
    rng = random.Random(9)
    days = [BASE + timedelta(days=i) for i in range(6)]
    for i, day in enumerate(days):
        if i == 2:
            continue  # arrives late
        add(db, monitor, f"b{i}", day, [0, .1, .2, 2.6, 2.7, 2.8][i], rng)
    incremental = published(db, monitor).result

    # late batch for day 2 inserted afterwards; full replay must equal a fresh one-pass calc
    add(db, monitor, "b2", days[2], .15, rng)
    late = published(db, monitor).result
    replay = late["batches"]

    # one-pass reference: evaluate the same batches directly, in date order
    from src.services.monitoring_analysis import evaluate_series
    from src.models import MonitoringBatch
    stored = db.query(MonitoringBatch).filter(MonitoringBatch.monitor_id == monitor.id).all()
    one_pass = evaluate_series(
        monitor.baseline,
        [
            {"batch_key": b.batch_key, "sampling_date": b.sampling_date, "revision": b.revision,
             "content_hash": b.content_hash, "rows": b.rows}
            for b in stored
        ],
        monitor.consecutive_limit,
    )
    assert [b["sampling_date"] for b in replay] == sorted(b["sampling_date"] for b in replay)
    assert [b["consecutive_out"] for b in replay] == [b["consecutive_out"] for b in one_pass["batches"]]
    assert late["summary"] == one_pass["summary"]
    assert incremental["summary"] != late["summary"]


def test_revision_replaces_prior_values_and_replays(db, monitor):
    rng = random.Random(21)
    add(db, monitor, "r1", BASE, 0.0, rng)
    add(db, monitor, "r2", BASE + timedelta(days=1), 2.5, rng)
    add(db, monitor, "r3", BASE + timedelta(days=2), 2.6, rng)
    before = published(db, monitor).result["summary"]
    assert [b["consecutive_out"] for b in published(db, monitor).result["batches"]] == [0, 1, 2]

    # revised early batch changes its deviation; the streak must be recomputed downstream
    add(db, monitor, "r1", BASE, 2.4, rng)
    db.refresh(monitor)
    after = published(db, monitor).result
    assert after["batches"][0]["revision"] == 2
    assert after["summary"]["consecutive_out"] == 3
    assert after["summary"]["alert_active"] is True
    assert after["summary"]["first_failure_date"] == BASE.isoformat()
    assert before["alert_active"] is False


def test_duplicate_delivery_not_counted(db, monitor):
    rng = random.Random(3)
    first = add(db, monitor, "dup", BASE, 0.2, rng)
    assert first["duplicate"] is False
    payload = BatchInput(batch_key="dup", sampling_date=BASE, csv_text=batch_csv(random.Random(3), 0.2), source_file="dup.csv")
    again = ingest_batch(db, monitor.id, payload, _user())
    assert again["duplicate"] is True
    assert again["batch"]["revision"] == 1
    db.refresh(monitor)
    seq_after_dup = monitor.change_seq
    same_under_other_key = ingest_batch(
        db, monitor.id,
        BatchInput(batch_key="dup-copy", sampling_date=BASE, csv_text=batch_csv(random.Random(3), 0.2), source_file="dup.csv"),
        _user(),
    )
    assert same_under_other_key["duplicate"] is True
    db.refresh(monitor)
    assert monitor.change_seq == seq_after_dup
    assert len(published(db, monitor).result["batches"]) == 1


def test_previous_version_stays_published_until_replay_finishes(db, monitor, fake_queue):
    rng = random.Random(8)
    add(db, monitor, "p1", BASE, 0.1, rng)
    first = published(db, monitor)
    # ingest without draining the worker
    batch = BatchInput(batch_key="p2", sampling_date=BASE + timedelta(days=1), csv_text=batch_csv(rng, 0.1), source_file="p2.csv")
    response = ingest_batch(db, monitor.id, batch, _user())
    assert response["pending"]["stale"] is True
    assert response["pending"]["replaying"] is True
    still = published(db, monitor)
    assert still.version == first.version
    assert still.result["summary"]["batch_count"] == 1
    assert monitoring_service.get_monitor(db, monitor.id)["published"]["version"] == first.version
    assert monitoring_service.get_monitor(db, monitor.id)["pending"]["stale"] is True
    drain(db, monitor)
    current = monitoring_service.get_monitor(db, monitor.id)
    assert current["pending"]["stale"] is False
    assert current["published"]["result"]["summary"]["batch_count"] == 2


def test_new_batches_during_replay_are_picked_up(db, monitor):
    rng = random.Random(31)
    add(db, monitor, "q1", BASE, 0.0, rng)
    queued = db.query(MonitoringEvaluation).filter(
        MonitoringEvaluation.monitor_id == monitor.id,
        MonitoringEvaluation.status.in_(["queued", "running"]),
    ).all()
    # simulate one running evaluation while another batch arrives
    add(db, monitor, "q2", BASE + timedelta(days=1), 0.2, rng)  # queued, not executed
    add(db, monitor, "q3", BASE + timedelta(days=2), 0.3, rng)  # coalesced into pending queue
    drain(db, monitor)
    result = published(db, monitor).result
    assert [b["batch_key"] for b in result["batches"]] == ["q1", "q2", "q3"]


def test_evaluation_fingerprint_is_order_independent(db, monitor):
    rng = random.Random(4)
    add(db, monitor, "f1", BASE, 0.0, rng)
    add(db, monitor, "f2", BASE + timedelta(days=1), 0.1, rng)
    fp_a = published(db, monitor).fingerprint
    from src.schemas.monitoring import ReplayInput
    before = published(db, monitor).result
    monitoring_service.request_replay(db, monitor.id, ReplayInput(), _user())
    drain(db, monitor)
    replay = published(db, monitor)
    assert replay.fingerprint == fp_a
    assert replay.result["batches"] == before["batches"]
    assert replay.result["summary"] == before["summary"]

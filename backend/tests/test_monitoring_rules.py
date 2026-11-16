from datetime import date

import pytest

from src.schemas.monitoring import BatchInput, MonitorInput
from src.services import monitoring_service
from src.services.monitoring_analysis import evaluate_series
from src.services.monitoring_service import create_monitor, execute_evaluation, ingest_batch, request_replay
from src.schemas.monitoring import ReplayInput

from tests.test_monitoring import BASE, _user, add, drain, make_baseline, published


@pytest.fixture()
def monitor(db):
    run = make_baseline(db)
    create_monitor(db, MonitorInput(name="规则监测", baseline_run_id=run.id, consecutive_limit=3), _user())
    from src.models import MonitoringProfile
    mon = db.query(MonitoringProfile).filter(MonitoringProfile.name == "规则监测").one()
    drain(db, mon)
    return mon


def make_csv(rows):
    lines = ["sample_code,reference_value,candidate_value"]
    lines += [f"S{i:03d},{x},{y}" for i, (x, y) in enumerate(rows)]
    return "\n".join(lines) + "\n"


def test_incomparable_batch_neither_clears_nor_extends_streak(db, monitor):
    import random
    rng = random.Random(77)
    days = [BASE + __import__("datetime").timedelta(days=i) for i in range(6)]
    for i in (0, 1, 2):
        add(db, monitor, f"s{i}", days[i], 2.6, rng)
    assert published(db, monitor).result["summary"]["alert_active"] is True
    # every reading outside the frozen X range -> not comparable
    outside = [(99.0, 99.5), (98.5, 99.0), (97.0, 97.5), (96.0, 96.5), (99.5, 99.8)]
    response = ingest_batch(
        db, monitor.id,
        BatchInput(batch_key="s3", sampling_date=days[3], csv_text=make_csv(outside), source_file="s3.csv"),
        _user(),
    )
    assert response["duplicate"] is False
    drain(db, monitor)
    result = published(db, monitor).result
    assert result["batches"][-1]["status"] == "not_comparable"
    assert result["batches"][-1]["consecutive_out"] == 3
    assert result["summary"]["alert_active"] is True
    # a comparable in-range batch is what clears the alert
    add(db, monitor, "s4", days[4], 0.0, rng)
    assert published(db, monitor).result["summary"]["alert_active"] is False


def test_bad_batch_does_not_change_published_version(db, monitor):
    import random
    rng = random.Random(12)
    add(db, monitor, "g1", BASE, 0.1, rng)
    before = published(db, monitor)
    with pytest.raises(Exception):
        ingest_batch(
            db, monitor.id,
            BatchInput(batch_key="g2", sampling_date=BASE, csv_text="sample_code,reference_value,candidate_value\nA,1,1\n", source_file="x.csv"),
            _user(),
        )
    db.refresh(monitor)
    after = published(db, monitor)
    assert after.version == before.version
    assert monitor.change_seq == before.applied_seq


def test_replay_from_date_is_recorded(db, monitor):
    import random
    rng = random.Random(13)
    days = [BASE + __import__("datetime").timedelta(days=i) for i in range(3)]
    for i in range(3):
        add(db, monitor, f"v{i}", days[i], 0.0, rng)
    response = request_replay(db, monitor.id, ReplayInput(from_date=days[1], reason="late"), _user())
    assert response["evaluation"]["replay_from"] == days[1].isoformat()
    drain(db, monitor)
    latest = published(db, monitor)
    assert latest.replay_from == days[1]
    assert latest.result["replay_from"] == days[1].isoformat()


def test_series_ordering_ignores_arrival_order(db, monitor):
    rng = __import__("random").Random(99)
    keys = ["c", "a", "b"]
    days = [BASE, BASE, BASE, BASE + __import__("datetime").timedelta(days=1)]
    for key, dev in zip(keys, [2.6, 2.5, 2.7]):
        add(db, monitor, key, BASE, dev, rng)
    add(db, monitor, "d", BASE + __import__("datetime").timedelta(days=1), 2.8, rng)
    result = published(db, monitor).result
    order = [(b["sampling_date"], b["batch_key"]) for b in result["batches"]]
    assert order == sorted(order)
    # three out-of-limit batches on the same date are all consecutive, regardless of key order
    assert result["summary"]["consecutive_out"] == 4
    assert result["summary"]["alert_active"] is True


def _stored_inputs(db, monitor):
    from src.models import MonitoringBatch
    rows = db.query(MonitoringBatch).filter(MonitoringBatch.monitor_id == monitor.id).all()
    return [
        {"batch_key": b.batch_key, "sampling_date": b.sampling_date, "revision": b.revision,
         "content_hash": b.content_hash, "rows": b.rows}
        for b in sorted(rows, key=lambda b: (b.sampling_date, b.batch_key))
    ]


def test_interval_replay_equals_full_recompute(db, monitor):
    import datetime
    import random
    rng = random.Random(41)
    days = [BASE + datetime.timedelta(days=i) for i in range(6)]
    for i in range(4):
        add(db, monitor, f"i{i}", days[i], 0.0, rng)
    # revising day 1 keeps only the day-0 prefix reusable
    add(db, monitor, "i1", days[1], 2.5, rng)
    result = published(db, monitor).result
    assert result["replay"]["scope"] == "interval"
    assert result["replay"]["reused_batches"] == 1
    suffix = evaluate_series(monitor.baseline, _stored_inputs(db, monitor), monitor.consecutive_limit)
    assert result["batches"] == suffix["batches"]
    assert result["summary"] == suffix["summary"]

    # an arrival after the last date only touches the tail
    add(db, monitor, "i9", days[5], 2.6, rng)
    result = published(db, monitor).result
    assert result["replay"]["scope"] == "interval"
    assert result["replay"]["reused_batches"] == 4
    suffix = evaluate_series(monitor.baseline, _stored_inputs(db, monitor), monitor.consecutive_limit)
    assert result["batches"] == suffix["batches"]
    assert result["summary"] == suffix["summary"]


def test_prefix_mismatch_falls_back_to_full(db, monitor):
    import random
    rng = random.Random(52)
    add(db, monitor, "z1", BASE, 0.0, rng)
    add(db, monitor, "z2", BASE + __import__("datetime").timedelta(days=1), 0.0, rng)
    tampered = published(db, monitor).result
    tampered["batches"][0]["content_hash"] = "00"
    naive = evaluate_series(
        monitor.baseline, _stored_inputs(db, monitor), monitor.consecutive_limit,
        replay_from=BASE + __import__("datetime").timedelta(days=1), previous=tampered,
    )
    assert naive["replay"]["scope"] == "full"
    assert naive["replay"]["reused_batches"] == 0
    assert naive["summary"] == evaluate_series(
        monitor.baseline, _stored_inputs(db, monitor), monitor.consecutive_limit
    )["summary"]


def test_first_failure_date_survives_clearing(db, monitor):
    import datetime
    import random
    rng = random.Random(61)
    days = [BASE + datetime.timedelta(days=i) for i in range(6)]
    for i in range(3):
        add(db, monitor, f"a{i}", days[i], 2.6, rng)
    result = published(db, monitor).result
    assert result["summary"]["alert_active"] is True
    assert result["summary"]["first_failure_date"] == days[0].isoformat()
    first_basis = [b["batch_key"] for b in result["summary"]["first_failure_batches"]]

    add(db, monitor, "a3", days[3], 0.0, rng)
    cleared = published(db, monitor).result
    assert cleared["summary"]["alert_active"] is False
    assert cleared["summary"]["first_failure_date"] == days[0].isoformat()
    assert [b["batch_key"] for b in cleared["summary"]["first_failure_batches"]] == first_basis
    assert cleared["summary"]["current_failure_batches"] == []
    assert cleared["summary"]["current_streak_start"] is None

    # a new later alert does not move the historical first failure date
    for i in (4, 5):
        add(db, monitor, f"b{i}", days[i], 2.7, rng)
    add(db, monitor, "b6", BASE + datetime.timedelta(days=6), 2.8, rng)
    again = published(db, monitor).result
    assert again["summary"]["alert_active"] is True
    assert again["summary"]["first_failure_date"] == days[0].isoformat()
    assert again["summary"]["current_streak_start"] == days[4].isoformat()


def test_same_date_streaks_are_attributed_to_the_right_block(db, monitor):
    import datetime
    import random
    rng = random.Random(71)
    day = BASE
    # three failing batches on day 0 trigger the alert, then one passing batch
    for key in ("m1", "m2", "m3"):
        add(db, monitor, key, day, 2.6, rng)
    add(db, monitor, "m4", day, 0.0, rng)
    # a new streak starts on the same sampling date and reaches the threshold
    for key in ("n1", "n2", "n3"):
        add(db, monitor, key, day, 2.7, rng)
    result = published(db, monitor).result
    assert result["summary"]["first_failure_date"] == day.isoformat()
    assert [b["batch_key"] for b in result["summary"]["first_failure_batches"]] == ["m1", "m2", "m3"]
    assert [b["batch_key"] for b in result["summary"]["current_failure_batches"]] == ["n1", "n2", "n3"]
    assert result["summary"]["alert_active"] is True

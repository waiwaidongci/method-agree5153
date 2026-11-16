"""Deterministic evaluation of independent daily batches against a frozen baseline.

The published monitoring result is a pure function of:
  * the frozen baseline snapshot (Deming relation / observed X range / allowance), and
  * the ordered set of batch revisions, sorted by (sampling_date, batch_key).

Replaying a late or revised batch therefore never depends on arrival order.  A
bounded replay reuses the unchanged prefix (batches sampled before the affected
date) whenever that prefix still matches the previously published rows, and
recomputes the interval from the replay date onward.  If the prefix cannot be
proven unchanged the whole series is recomputed.  Both paths produce exactly the
same rows and summary as a single pass over the final input set.
"""
from datetime import date

ALGORITHM = "monitoring-v1"
MIN_COMPARABLE = 5


def _as_date(value):
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def _batch_stats(batch, baseline):
    low, high = (float(v) for v in baseline["x_range"])
    points = []
    for row in batch["rows"]:
        if isinstance(row, dict):
            x, y = float(row["x"]), float(row["y"])
        else:  # [sample_code, reference, candidate]
            x, y = float(row[1]), float(row[2])
        if low - 1e-9 <= x <= high + 1e-9:
            points.append((x, y))

    n_total = len(batch["rows"])
    n = len(points)
    stats = {
        "batch_key": batch["batch_key"],
        "sampling_date": _as_date(batch["sampling_date"]).isoformat(),
        "revision": int(batch.get("revision", 1)),
        "content_hash": batch.get("content_hash", ""),
        "n_total": n_total,
        "n_in_range": n,
        "coverage": (n / n_total) if n_total else 0.0,
        "raw_bias": None,
        "sd_difference": None,
        "bias_delta": None,
        "bias_delta_ci": None,
        "status": "not_comparable",
        "out_of_limit": False,
        "consecutive_out": 0,
        "alert": False,
        "streak_start": None,
        "streak_index": None,
    }
    if n < MIN_COMPARABLE:
        return stats

    diffs = [y - x for x, y in points]
    raw_bias = sum(diffs) / n
    sd = (sum((d - raw_bias) ** 2 for d in diffs) / (n - 1)) ** 0.5 if n > 1 else 0.0
    delta = raw_bias - float(baseline["bias"])
    half = 1.96 * sd / (n ** 0.5)
    stats.update({
        "raw_bias": raw_bias,
        "sd_difference": sd,
        "bias_delta": delta,
        "bias_delta_ci": [delta - half, delta + half],
        "status": "comparable",
    })
    return stats


def _evidence(rows, start_index, end_index):
    return [
        {"batch_key": row["batch_key"], "sampling_date": row["sampling_date"], "revision": row["revision"]}
        for row in rows[start_index:end_index]
        if row["out_of_limit"]
    ]


def _streak_evidence(rows, start_index):
    """Out-of-limit batches from a streak, anchored by row index.

    Index anchoring keeps several batches sharing one sampling date attributed to
    the streak they actually belong to.
    """
    if start_index is None or not 0 <= start_index < len(rows):
        return []
    end = start_index
    while end < len(rows) and (rows[end]["out_of_limit"] or rows[end]["status"] != "comparable"):
        end += 1
    return _evidence(rows, start_index, end)


def _walk(ordered, baseline, consecutive_limit, seed_rows=None, seed_streak=0, seed_start=None, seed_index=None):
    """Walk the ordered batches, extending an optional reused prefix."""
    allowance = float(baseline["allowance"])
    rows = list(seed_rows or [])
    streak = int(seed_streak)
    streak_start = seed_start
    streak_index = seed_index

    for batch in ordered:
        stats = _batch_stats(batch, baseline)
        stats["out_of_limit"] = (
            stats["status"] == "comparable"
            and abs(stats["bias_delta"]) > allowance
        )
        if stats["out_of_limit"]:
            if streak == 0:
                streak_start = stats["sampling_date"]
                streak_index = len(rows)
            streak += 1
        elif stats["status"] == "comparable":
            streak = 0
            streak_start = None
            streak_index = None
        # A batch with too few readings in the baseline range is neither in nor
        # out of limit; it must not silently clear an alert, so the streak carries.
        stats["consecutive_out"] = streak
        stats["streak_start"] = streak_start
        stats["streak_index"] = streak_index
        stats["alert"] = streak >= int(consecutive_limit)
        rows.append(stats)
    return rows, streak, streak_start, streak_index


def _reusable_prefix(prefix_batches, prefix_rows):
    """True when the given stored rows still describe exactly these prefix batches."""
    if prefix_rows is None or len(prefix_rows) != len(prefix_batches):
        return False
    for batch, row in zip(prefix_batches, prefix_rows):
        if (
            row.get("batch_key") != batch["batch_key"]
            or row.get("sampling_date") != _as_date(batch["sampling_date"]).isoformat()
            or int(row.get("revision", 0)) != int(batch.get("revision", 1))
            or row.get("content_hash", "") != batch.get("content_hash", "")
        ):
            return False
    return True


def evaluate_series(baseline, batches, consecutive_limit, replay_from=None, previous=None):
    """Return per-batch deviation, consecutive out-of-limit state and a summary.

    ``previous`` is the previously published result; when ``replay_from`` is set and
    the untouched prefix still matches, only the interval from that date is
    recomputed.  The outcome is identical to a full single pass in either case.
    """
    allowance = float(baseline["allowance"])
    ordered = sorted(
        batches,
        key=lambda b: (_as_date(b["sampling_date"]), b["batch_key"]),
    )
    replay_floor = _as_date(replay_from) if replay_from else None

    seed_rows, seed_streak, seed_start, seed_index = [], 0, None, None
    recomputed_from = None
    if replay_floor is not None and previous:
        split = next(
            (
                index
                for index, batch in enumerate(ordered)
                if _as_date(batch["sampling_date"]) >= replay_floor
            ),
            len(ordered),
        )
        prefix_batches = ordered[:split]
        prefix_rows = previous.get("batches", [])[:split]
        if split and _reusable_prefix(prefix_batches, prefix_rows):
            seed_rows = prefix_rows
            seed_streak = seed_rows[-1]["consecutive_out"]
            seed_start = seed_rows[-1]["streak_start"]
            seed_index = seed_rows[-1].get("streak_index")
            recomputed_from = split
        ordered_for_walk = ordered[split:]
    else:
        ordered_for_walk = ordered

    if recomputed_from is None:
        seed_rows, seed_streak, seed_start, seed_index = [], 0, None, None
        ordered_for_walk = ordered
        recomputed_from = 0

    rows, streak, streak_start, streak_index = _walk(
        ordered_for_walk, baseline, consecutive_limit, seed_rows, seed_streak, seed_start, seed_index
    )

    failures = [r for r in rows if r["out_of_limit"]]
    not_comparable = [r for r in rows if r["status"] != "comparable"]
    alert_active = bool(rows and rows[-1]["alert"])
    latest = rows[-1] if rows else None
    first_alert = next((r for r in rows if r["alert"]), None)
    first_failure_date = first_alert["streak_start"] if first_alert else None
    first_failure_batches = _streak_evidence(rows, first_alert["streak_index"]) if first_alert else []
    summary = {
        "batch_count": len(rows),
        "out_of_limit_count": len(failures),
        "not_comparable_count": len(not_comparable),
        "alert_active": alert_active,
        "consecutive_limit": int(consecutive_limit),
        "consecutive_out": streak,
        # Stable historical facts: the earliest date whose streak reached the alert
        # threshold, fixed at its triggering batch. Later in-range batches end the
        # active alert but do not erase when qualification delivery first failed.
        "first_failure_date": first_failure_date,
        "first_failure_batches": first_failure_batches,
        "current_streak_start": streak_start,
        "current_failure_batches": _streak_evidence(rows, streak_index) if alert_active else [],
        "evaluated_through": latest["sampling_date"] if latest else None,
        "latest_batch_key": latest["batch_key"] if latest else None,
        "latest_deviation": latest["bias_delta"] if latest else None,
        "latest_status": latest["status"] if latest else None,
    }
    return {
        "algorithm": ALGORITHM,
        "baseline_bias": float(baseline["bias"]),
        "allowance": allowance,
        "x_range": [float(v) for v in baseline["x_range"]],
        "consecutive_limit": int(consecutive_limit),
        "replay": {
            "requested_from": replay_floor.isoformat() if replay_floor else None,
            "recomputed_from": (
                rows[recomputed_from]["sampling_date"]
                if recomputed_from < len(rows)
                else None
            ),
            "scope": "interval" if replay_floor and recomputed_from else "full",
            "reused_batches": recomputed_from,
        },
        "batches": rows,
        "summary": summary,
    }

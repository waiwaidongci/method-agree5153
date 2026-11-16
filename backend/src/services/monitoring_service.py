"""Incremental ingestion, versioned replay and publication of monitoring results."""
import csv
import hashlib
import io
import json
from datetime import date, datetime

from fastapi import HTTPException
from pydantic import ValidationError
from redis import Redis
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from src.config.settings import REDIS_URL
from src.constants.enums import MonitoringStatus, RunStatus
from src.models import (
    ComparisonRun,
    Instrument,
    MonitoringBatch,
    MonitoringBatchRevision,
    MonitoringEvaluation,
    MonitoringProfile,
    PairDataset,
)
from src.schemas.reading import ReadingInput
from src.services.monitoring_analysis import ALGORITHM, MIN_COMPARABLE, evaluate_series

queue = Redis.from_url(REDIS_URL)

BATCH_MIN_ROWS = MIN_COMPARABLE
BATCH_MAX_ROWS = 5000
MONITOR_QUEUE = "optics:monitors"


def _iso(value):
    return value.isoformat() if value else None


def _as_iso(value):
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _content_hash(sampling_date, rows):
    canonical = {
        "sampling_date": sampling_date.isoformat(),
        "rows": sorted(
            ([r.sample_code, r.reference_value, r.candidate_value] for r in rows),
            key=lambda r: r[0],
        ),
    }
    return hashlib.sha256(
        json.dumps(canonical, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def _evaluation_fingerprint(monitor, batches):
    """Identity of the evaluated input set; a bounded replay yields the same value."""
    canonical = {
        "algorithm": ALGORITHM,
        "baseline_fingerprint": monitor.baseline.get("run_fingerprint", ""),
        "baseline": {
            "bias": monitor.baseline.get("bias"),
            "x_range": monitor.baseline.get("x_range"),
            "allowance": monitor.baseline.get("allowance"),
        },
        "consecutive_limit": monitor.consecutive_limit,
        "batches": sorted(
            ([b["batch_key"], _as_iso(b["sampling_date"]), b["revision"], b["content_hash"]] for b in batches),
            key=lambda b: (b[1], b[0]),
        ),
    }
    return hashlib.sha256(
        json.dumps(canonical, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def _latest_published(db, monitor_id):
    return db.scalar(
        select(MonitoringEvaluation)
        .where(
            MonitoringEvaluation.monitor_id == monitor_id,
            MonitoringEvaluation.status == MonitoringStatus.COMPLETED,
        )
        .order_by(MonitoringEvaluation.version.desc())
    )


def _open_evaluations(db, monitor_id):
    return db.scalars(
        select(MonitoringEvaluation)
        .where(
            MonitoringEvaluation.monitor_id == monitor_id,
            MonitoringEvaluation.status.in_([MonitoringStatus.QUEUED, MonitoringStatus.RUNNING]),
        )
        .order_by(MonitoringEvaluation.version)
    ).all()


def _pending_replay_from(db, monitor, open_items):
    candidates = [monitor.pending_from] + [item.replay_from for item in open_items]
    candidates = [c for c in candidates if c is not None]
    if candidates:
        return _iso(min(candidates))
    earliest = db.scalar(
        select(func.min(MonitoringBatch.sampling_date)).where(
            MonitoringBatch.monitor_id == monitor.id,
            MonitoringBatch.active.is_(True),
        )
    )
    return _iso(earliest)


def pending_state(db, monitor):
    """Whether the published version already includes every accepted batch."""
    published = _latest_published(db, monitor.id)
    open_items = _open_evaluations(db, monitor.id)
    applied_seq = published.applied_seq if published else -1
    stale = monitor.change_seq > applied_seq
    return {
        "version": published.version if published else None,
        "applied_seq": applied_seq,
        "change_seq": monitor.change_seq,
        "stale": stale,
        "replaying": bool(open_items),
        "replay_from": _pending_replay_from(db, monitor, open_items) if stale else None,
        "queued_versions": [item.version for item in open_items],
    }


def enqueue_evaluation(db, monitor, replay_from=None, reason="", requested_by=""):
    """Queue one replay of the affected interval; concurrent arrivals coalesce."""
    queued = db.scalars(
        select(MonitoringEvaluation)
        .where(
            MonitoringEvaluation.monitor_id == monitor.id,
            MonitoringEvaluation.status == MonitoringStatus.QUEUED,
        )
        .order_by(MonitoringEvaluation.version.desc())
    ).first()
    if queued:
        queued.requested_seq = monitor.change_seq
        if replay_from and (queued.replay_from is None or replay_from < queued.replay_from):
            queued.replay_from = replay_from
        if reason:
            queued.reason = reason[:300]
        db.commit()
        return queued

    version = (
        db.scalar(
            select(func.max(MonitoringEvaluation.version)).where(
                MonitoringEvaluation.monitor_id == monitor.id
            )
        )
        or 0
    ) + 1
    evaluation = MonitoringEvaluation(
        monitor_id=monitor.id,
        version=version,
        status=MonitoringStatus.QUEUED,
        requested_seq=monitor.change_seq,
        applied_seq=-1,
        replay_from=replay_from,
        reason=str(reason)[:300],
        requested_by=requested_by,
    )
    db.add(evaluation)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return enqueue_evaluation(db, monitor, replay_from)
    queue.lpush(MONITOR_QUEUE, str(evaluation.id))
    return evaluation


def _baseline_snapshot(run, dataset):
    result = run.result
    return {
        "run_id": run.id,
        "run_name": run.name,
        "run_fingerprint": result["fingerprint"],
        "dataset_id": dataset.id,
        "dataset_name": dataset.name,
        "instrument_ids": [dataset.reference_id, dataset.candidate_id],
        "instrument_codes": result.get("instrument_codes", []),
        "bias": float(result["bias"]),
        "sd_difference": float(result["sd_difference"]),
        "loa_lower": float(result["loa_lower"]),
        "loa_upper": float(result["loa_upper"]),
        "x_range": [float(v) for v in result["x_range"]],
        "y_range": [float(v) for v in result["y_range"]],
        "allowance": float(run.allowance),
        "lambda_ratio": float(run.lambda_ratio),
        "n": int(result["n"]),
        "raw_eligible": bool(result.get("raw_eligible", False)),
        "frozen_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "algorithm": "monitoring-baseline-v1",
    }


def create_monitor(db, data, user):
    run = db.get(ComparisonRun, data.baseline_run_id)
    if not run:
        raise HTTPException(404, "基线研究不存在")
    if run.status != RunStatus.COMPLETED:
        raise HTTPException(409, "仅已完成的研究可以冻结为监测基线")
    dataset = db.get(PairDataset, run.dataset_id)
    if not dataset:
        raise HTTPException(404, "基线数据集不存在")
    if db.scalar(select(MonitoringProfile).where(MonitoringProfile.baseline_run_id == run.id)):
        raise HTTPException(409, "该研究已冻结为监测基线，不能重复冻结")
    if db.scalar(select(MonitoringProfile).where(MonitoringProfile.name == data.name)):
        raise HTTPException(409, "监测计划名称已存在")
    monitor = MonitoringProfile(
        name=data.name,
        baseline_run_id=run.id,
        consecutive_limit=data.consecutive_limit,
        baseline=_baseline_snapshot(run, dataset),
        note=data.note,
        created_by=user.email,
        change_seq=0,
    )
    db.add(monitor)
    db.commit()
    # The frozen baseline itself is the first published version (no batches yet).
    enqueue_evaluation(db, monitor, None, reason="冻结基线", requested_by=user.email)
    return get_monitor(db, monitor.id)


def _monitor_summary(monitor, published):
    return {
        "id": monitor.id,
        "name": monitor.name,
        "baseline_run_id": monitor.baseline_run_id,
        "consecutive_limit": monitor.consecutive_limit,
        "baseline": monitor.baseline,
        "note": monitor.note,
        "created_by": monitor.created_by,
        "created_at": _iso(monitor.created_at),
        "published": {
            "version": published.version if published else None,
            "completed_at": _iso(published.completed_at) if published else None,
            "replay_from": _iso(published.replay_from) if published else None,
            "fingerprint": published.fingerprint if published else "",
            "summary": published.result.get("summary", {}) if published else {},
        },
    }


def list_monitors(db):
    rows = db.scalars(select(MonitoringProfile).order_by(MonitoringProfile.id.desc())).all()
    counts = dict(
        db.execute(
            select(MonitoringBatch.monitor_id, func.count(MonitoringBatch.id))
            .where(MonitoringBatch.active.is_(True))
            .group_by(MonitoringBatch.monitor_id)
        ).all()
    )
    output = []
    for monitor in rows:
        item = _monitor_summary(monitor, _latest_published(db, monitor.id))
        item["batch_count"] = counts.get(monitor.id, 0)
        item["pending"] = pending_state(db, monitor)
        output.append(item)
    return output


def get_monitor(db, id):
    monitor = db.get(MonitoringProfile, id)
    if not monitor:
        raise HTTPException(404, "监测计划不存在")
    published = _latest_published(db, monitor.id)
    batches = db.scalars(
        select(MonitoringBatch)
        .where(MonitoringBatch.monitor_id == monitor.id, MonitoringBatch.active.is_(True))
        .order_by(MonitoringBatch.sampling_date, MonitoringBatch.batch_key)
    ).all()
    result = dict(published.result) if published else {}
    return {
        **_monitor_summary(monitor, published),
        "published": {
            "version": published.version if published else None,
            "completed_at": _iso(published.completed_at) if published else None,
            "replay_from": _iso(published.replay_from) if published else None,
            "fingerprint": published.fingerprint if published else "",
            "result": result,
        },
        "batches": [
            {
                "id": batch.id,
                "batch_key": batch.batch_key,
                "sampling_date": _iso(batch.sampling_date),
                "revision": batch.revision,
                "content_hash": batch.content_hash,
                "row_count": batch.row_count,
                "source_file": batch.source_file,
                "note": batch.note,
                "received_by": batch.received_by,
                "first_received_at": _iso(batch.first_received_at),
                "updated_at": _iso(batch.updated_at),
            }
            for batch in batches
        ],
        "pending": pending_state(db, monitor),
    }


def list_evaluations(db, monitor_id):
    if not db.get(MonitoringProfile, monitor_id):
        raise HTTPException(404, "监测计划不存在")
    rows = db.scalars(
        select(MonitoringEvaluation)
        .where(MonitoringEvaluation.monitor_id == monitor_id)
        .order_by(MonitoringEvaluation.version.desc())
        .limit(50)
    ).all()
    return [
        {
            "id": item.id,
            "version": item.version,
            "status": item.status,
            "replay_from": _iso(item.replay_from),
            "reason": item.reason,
            "requested_by": item.requested_by,
            "requested_seq": item.requested_seq,
            "applied_seq": item.applied_seq,
            "fingerprint": item.fingerprint,
            "error": item.error,
            "created_at": _iso(item.created_at),
            "started_at": _iso(item.started_at),
            "completed_at": _iso(item.completed_at),
        }
        for item in rows
    ]


def _validate_rows(db, monitor, rows):
    run = db.get(ComparisonRun, monitor.baseline_run_id)
    dataset = db.get(PairDataset, run.dataset_id)
    reference = db.get(Instrument, dataset.reference_id)
    candidate = db.get(Instrument, dataset.candidate_id)
    if any(
        not reference.lower <= row.reference_value <= reference.upper
        or not candidate.lower <= row.candidate_value <= candidate.upper
        for row in rows
    ):
        raise HTTPException(422, "读数超出仪器登记量程")


def _parse_csv(csv_text):
    try:
        rows = [ReadingInput.model_validate(row) for row in csv.DictReader(io.StringIO(csv_text))]
    except (ValidationError, ValueError, csv.Error) as exc:
        raise HTTPException(422, f"批次CSV校验失败：{str(exc)[:300]}")
    if not BATCH_MIN_ROWS <= len(rows) <= BATCH_MAX_ROWS:
        raise HTTPException(422, f"每个批次需要{BATCH_MIN_ROWS}–{BATCH_MAX_ROWS}对独立样本")
    if len({row.sample_code for row in rows}) != len(rows):
        raise HTTPException(422, "批次内样本编号不得重复；每片试件只保留一对读数")
    return rows


def _ingest_response(db, monitor, batch, duplicate, changed):
    db.refresh(monitor)
    return {
        "duplicate": duplicate,
        "changed": changed,
        "batch": {
            "id": batch.id,
            "batch_key": batch.batch_key,
            "sampling_date": _iso(batch.sampling_date),
            "revision": batch.revision,
            "content_hash": batch.content_hash,
            "row_count": batch.row_count,
        },
        "pending": pending_state(db, monitor),
    }


def ingest_batch(db, monitor_id, data, user):
    monitor = db.get(MonitoringProfile, monitor_id)
    if not monitor:
        raise HTTPException(404, "监测计划不存在")
    rows = _parse_csv(data.csv_text)
    _validate_rows(db, monitor, rows)

    digest = _content_hash(data.sampling_date, rows)
    payload = [
        {"code": row.sample_code, "x": row.reference_value, "y": row.candidate_value}
        for row in rows
    ]
    existing = db.scalar(
        select(MonitoringBatch).where(
            MonitoringBatch.monitor_id == monitor.id,
            MonitoringBatch.batch_key == data.batch_key,
        )
    )
    if existing and existing.content_hash == digest:
        return _ingest_response(db, monitor, existing, duplicate=True, changed=False)

    # Same sampling date and identical readings delivered under another key is a
    # repeat delivery of the same independent batch: count it once.
    twin = db.scalar(
        select(MonitoringBatch).where(
            MonitoringBatch.monitor_id == monitor.id,
            MonitoringBatch.content_hash == digest,
            MonitoringBatch.active.is_(True),
        )
    )
    if twin:
        return _ingest_response(db, monitor, twin, duplicate=True, changed=False)

    if existing:
        replay_from = min(existing.sampling_date, data.sampling_date)
        existing.sampling_date = data.sampling_date
        existing.revision += 1
        existing.content_hash = digest
        existing.row_count = len(rows)
        existing.rows = payload
        existing.source_file = data.source_file
        existing.note = data.note
        existing.received_by = user.email
        existing.updated_at = datetime.utcnow()
        batch = existing
    else:
        replay_from = data.sampling_date
        batch = MonitoringBatch(
            monitor_id=monitor.id,
            batch_key=data.batch_key,
            sampling_date=data.sampling_date,
            revision=1,
            content_hash=digest,
            row_count=len(rows),
            rows=payload,
            source_file=data.source_file,
            note=data.note,
            received_by=user.email,
            first_received_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        db.add(batch)

    db.flush()
    db.add(
        MonitoringBatchRevision(
            batch_id=batch.id,
            revision=batch.revision,
            sampling_date=batch.sampling_date,
            content_hash=digest,
            row_count=len(rows),
            rows=payload,
            source_file=data.source_file,
            note=data.note,
            accepted_by=user.email,
        )
    )
    monitor.change_seq += 1
    monitor.pending_from = (
        replay_from
        if monitor.pending_from is None
        else min(monitor.pending_from, replay_from)
    )
    db.commit()
    enqueue_evaluation(
        db, monitor, monitor.pending_from,
        reason=f"批次{data.batch_key}第{batch.revision}版",
        requested_by=user.email,
    )
    return _ingest_response(db, monitor, batch, duplicate=False, changed=True)


def request_replay(db, monitor_id, data, user):
    monitor = db.get(MonitoringProfile, monitor_id)
    if not monitor:
        raise HTTPException(404, "监测计划不存在")
    earliest = db.scalar(
        select(func.min(MonitoringBatch.sampling_date)).where(
            MonitoringBatch.monitor_id == monitor.id,
            MonitoringBatch.active.is_(True),
        )
    )
    replay_from = data.from_date or earliest or date.today()
    evaluation = enqueue_evaluation(db, monitor, replay_from, reason=data.reason, requested_by=user.email)
    return {
        "evaluation": {
            "id": evaluation.id,
            "version": evaluation.version,
            "status": evaluation.status,
            "replay_from": _iso(evaluation.replay_from),
        },
        "pending": pending_state(db, monitor),
    }


def execute_evaluation(db, id):
    evaluation = db.get(MonitoringEvaluation, id)
    if not evaluation or evaluation.status not in (MonitoringStatus.QUEUED, MonitoringStatus.RUNNING):
        return
    evaluation.status = MonitoringStatus.RUNNING
    evaluation.started_at = datetime.utcnow()
    db.commit()
    try:
        monitor = db.get(MonitoringProfile, evaluation.monitor_id)
        if not monitor:
            raise ValueError("监测计划已被删除")
        applied_seq = monitor.change_seq
        replay_from = evaluation.replay_from
        # Later arrivals set their own pending_from; this run owns everything up to applied_seq.
        monitor.pending_from = None
        db.commit()

        batch_rows = db.scalars(
            select(MonitoringBatch)
            .where(MonitoringBatch.monitor_id == monitor.id, MonitoringBatch.active.is_(True))
            .order_by(MonitoringBatch.sampling_date, MonitoringBatch.batch_key)
        ).all()
        batch_inputs = [
            {
                "batch_key": batch.batch_key,
                "sampling_date": batch.sampling_date,
                "revision": batch.revision,
                "content_hash": batch.content_hash,
                "rows": batch.rows,
            }
            for batch in batch_rows
        ]
        previous = _latest_published(db, monitor.id)
        result = evaluate_series(
            monitor.baseline,
            batch_inputs,
            monitor.consecutive_limit,
            replay_from=replay_from,
            previous=previous.result if previous else None,
        )
        result["replay_from"] = _iso(replay_from)
        result["baseline"] = monitor.baseline
        evaluation.result = result
        evaluation.fingerprint = _evaluation_fingerprint(monitor, batch_inputs)
        evaluation.applied_seq = applied_seq
        evaluation.status = MonitoringStatus.COMPLETED
        evaluation.completed_at = datetime.utcnow()
        evaluation.error = ""
        db.commit()

        db.refresh(monitor)
        if monitor.change_seq > applied_seq:
            enqueue_evaluation(db, monitor, monitor.pending_from, reason="重放期间新到达批次")
        else:
            monitor.pending_from = None
            db.commit()
    except Exception as exc:
        db.rollback()
        evaluation = db.get(MonitoringEvaluation, id)
        evaluation.status = MonitoringStatus.FAILED
        evaluation.error = str(exc)[:600]
        evaluation.completed_at = datetime.utcnow()
        monitor = db.get(MonitoringProfile, evaluation.monitor_id)
        if monitor:
            missed = evaluation.replay_from
            if missed and (monitor.pending_from is None or missed < monitor.pending_from):
                monitor.pending_from = missed
            evaluation.applied_seq = -1
        db.commit()


def _safe_cell(value):
    text = "" if value is None else str(value)
    if text.startswith(("=", "+", "-", "@", "\t", "\r")):
        return "'" + text
    return text


def export_monitor(db, monitor_id):
    monitor = db.get(MonitoringProfile, monitor_id)
    if not monitor:
        raise HTTPException(404, "监测计划不存在")
    published = _latest_published(db, monitor.id)
    if not published:
        raise HTTPException(409, "监测结果尚未生成")
    result = published.result
    summary = result.get("summary", {})
    output = io.StringIO()
    output.write("﻿")
    writer = csv.writer(output)
    writer.writerow(["# monitor", _safe_cell(monitor.name)])
    writer.writerow(["# baseline_run_id", monitor.baseline_run_id])
    writer.writerow(["# baseline_fingerprint", monitor.baseline.get("run_fingerprint", "")])
    writer.writerow(["# published_version", published.version])
    writer.writerow(["# evaluation_fingerprint", published.fingerprint])
    writer.writerow(["# first_failure_date", summary.get("first_failure_date") or ""])
    writer.writerow([
        "# first_failure_batches",
        json.dumps(summary.get("current_failure_batches", []), ensure_ascii=False, sort_keys=True),
    ])
    writer.writerow(["# alert_active", "true" if summary.get("alert_active") else "false"])
    writer.writerow(["# consecutive_limit", monitor.consecutive_limit])
    writer.writerow(["# replay_scope", result.get("replay", {}).get("scope", "")])
    writer.writerow(["# replay_recomputed_from", result.get("replay", {}).get("recomputed_from") or ""])
    writer.writerow(["# qualification_conclusion", "approved qualifications keep their original conclusion"])
    writer.writerow([
        "batch_key",
        "sampling_date",
        "revision",
        "content_hash",
        "n_total",
        "n_in_range",
        "raw_bias",
        "bias_delta",
        "bias_delta_ci_low",
        "bias_delta_ci_high",
        "out_of_limit",
        "consecutive_out",
        "alert",
    ])
    for row in result.get("batches", []):
        ci = row.get("bias_delta_ci") or ["", ""]
        writer.writerow([
            _safe_cell(row["batch_key"]),
            row["sampling_date"],
            row["revision"],
            row["content_hash"],
            row["n_total"],
            row["n_in_range"],
            "" if row["raw_bias"] is None else f"{row['raw_bias']:.6f}",
            "" if row["bias_delta"] is None else f"{row['bias_delta']:.6f}",
            "" if ci[0] == "" else f"{ci[0]:.6f}",
            "" if ci[1] == "" else f"{ci[1]:.6f}",
            "true" if row["out_of_limit"] else "false",
            row["consecutive_out"],
            "true" if row["alert"] else "false",
        ])
    return output.getvalue()


def qualification_monitoring_summary(db, run_id):
    """Attach monitoring status to an approved qualification without changing it."""
    profiles = db.scalars(
        select(MonitoringProfile).where(MonitoringProfile.baseline_run_id == run_id)
    ).all()
    output = []
    for monitor in profiles:
        published = _latest_published(db, monitor.id)
        summary = published.result.get("summary", {}) if published else {}
        output.append({
            "id": monitor.id,
            "name": monitor.name,
            "published_version": published.version if published else None,
            "alert_active": bool(summary.get("alert_active")),
            "first_failure_date": summary.get("first_failure_date"),
            "basis_batches": summary.get("first_failure_batches", []),
            "current_streak_batches": summary.get("current_failure_batches", []),
            "conclusion_preserved": True,
        })
    return output

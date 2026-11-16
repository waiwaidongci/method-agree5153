from fastapi import APIRouter, Depends, Response
from src.config.database import get_db
from src.utils.auth import current_user, editor
from src.schemas.monitoring import MonitorInput, BatchInput, ReplayInput
from src.controllers.monitoring_controller import (
    list_monitors,
    get_monitor,
    create_monitor,
    ingest_batch,
    request_replay,
    list_evaluations,
    export_monitor,
)

router = APIRouter(prefix="/monitors")


@router.get("")
def index(db=Depends(get_db), user=Depends(current_user)):
    return list_monitors(db)


@router.post("", status_code=201)
def create(data: MonitorInput, db=Depends(get_db), user=Depends(editor)):
    return create_monitor(db, data, user)


@router.get("/{id}")
def detail(id: int, db=Depends(get_db), user=Depends(current_user)):
    return get_monitor(db, id)


@router.post("/{id}/batches", status_code=201)
def batch(id: int, data: BatchInput, db=Depends(get_db), user=Depends(editor)):
    return ingest_batch(db, id, data, user)


@router.post("/{id}/replay", status_code=202)
def replay(id: int, data: ReplayInput, db=Depends(get_db), user=Depends(editor)):
    return request_replay(db, id, data, user)


@router.get("/{id}/evaluations")
def evaluations(id: int, db=Depends(get_db), user=Depends(current_user)):
    return list_evaluations(db, id)


@router.get("/{id}/export")
def export(id: int, db=Depends(get_db), user=Depends(current_user)):
    return Response(
        export_monitor(db, id),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="optics-monitor-{id}.csv"'},
    )

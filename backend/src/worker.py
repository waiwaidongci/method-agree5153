import time
from pathlib import Path
from sqlalchemy import select
from redis import Redis
from src.config.database import Session
from src.config.settings import REDIS_URL
from src.models import ComparisonRun, MonitoringEvaluation
from src.constants.enums import RunStatus, MonitoringStatus
from src.services.run_service import execute_run
from src.services.monitoring_service import execute_evaluation
redis=Redis.from_url(REDIS_URL)
while True:
    Path('/tmp/heartbeat').touch()
    with Session() as db:
        run=db.scalar(select(ComparisonRun).where(ComparisonRun.status.in_([RunStatus.QUEUED,RunStatus.RUNNING])).order_by(ComparisonRun.id))
        if run:execute_run(db,run.id)
        else:
            evaluation=db.scalar(select(MonitoringEvaluation).where(MonitoringEvaluation.status.in_([MonitoringStatus.QUEUED,MonitoringStatus.RUNNING])).order_by(MonitoringEvaluation.id))
            if evaluation:execute_evaluation(db,evaluation.id)
            else:redis.brpop(['optics:runs','optics:monitors'],timeout=2)
    time.sleep(.1)

import random,json
from sqlalchemy import select,func
from src.config.database import Base,engine,Session
from src.models import User,Instrument,PairDataset,PairedReading,ComparisonRun,Qualification,MonitoringProfile,MonitoringBatch
from src.schemas.monitoring import MonitorInput,BatchInput
from src.services.monitoring_service import create_monitor,ingest_batch,execute_evaluation
from src.models import MonitoringEvaluation
from sqlalchemy import func as _count
from src.utils.security import hash_password
from src.services.run_service import execute_run

def seed():
    Base.metadata.create_all(engine);rng=random.Random(73)
    with Session() as db:
        for role,name in [('admin','程瓷 · 实验室负责人'),('analyst','江光 · 测量工程师'),('viewer','许澄 · 设备观察员')]:
            if not db.scalar(select(User).where(User.email==f'{role}@ciguang.demo')):db.add(User(email=f'{role}@ciguang.demo',name=name,role=role,password_hash=hash_password('Ciguang2026!')))
        for code,name,serial in [('CG-R01','参考反射率仪 R01','CG-2018-001'),('CG-N02','新型反射率仪 N02','CG-2026-042'),('CG-N03','便携反射率仪 N03','CG-2026-083')]:
            if not db.scalar(select(Instrument).where(Instrument.code==code)):db.add(Instrument(code=code,name=name,serial=serial,unit='反射率 %',lower=0,upper=100))
        db.commit();instruments=db.scalars(select(Instrument).order_by(Instrument.id)).all()
        specs=[('N02 · 稳定光源配对',instruments[1],.35,1.002,False),('N02 · 系统偏差复核',instruments[1],1.5,1.045,False),('N03 · 宽范围异方差',instruments[2],.2,1.008,True)]
        for idx,(name,candidate,intercept,slope,hetero) in enumerate(specs):
            dataset=db.scalar(select(PairDataset).where(PairDataset.name==name))
            if not dataset:
                dataset=PairDataset(name=name,reference_id=instruments[0].id,candidate_id=candidate.id,source_file='ci-optics-pairs.csv',note='高量程误差扩大；需关注异方差，整体界限仅作描述。' if hetero else '独立陶瓷试片，每片同日各测一次；单位为反射率百分数。')
                db.add(dataset);db.flush()
                for i in range(180):
                    latent=8+i*80/179
                    x=round(latent+rng.gauss(0,.22),4)
                    noise=(.12 if latent<48 else 1.7) if hetero else .25
                    y=round(intercept+slope*latent+rng.gauss(0,noise),4)
                    db.add(PairedReading(dataset_id=dataset.id,sample_code=f'CG{idx+1}-{i+1:03d}',reference_value=x,candidate_value=y))
                db.commit()
            runname=name+' / λ=1'
            if not db.scalar(select(ComparisonRun).where(ComparisonRun.name==runname)):
                run=ComparisonRun(name=runname,dataset_id=dataset.id,lambda_ratio=1,allowance=2,range_low=0,range_high=100,bootstrap_count=500,status='queued');db.add(run);db.commit();execute_run(db,run.id)
        first=db.scalar(select(ComparisonRun).order_by(ComparisonRun.id))
        if first and first.status=='completed' and not db.scalar(select(Qualification).where(Qualification.title=='N02稳定光源资格 · 10–85%')):
            db.add(Qualification(title='N02稳定光源资格 · 10–85%',run_id=first.id,mode='raw',scope_low=10,scope_high=85,note='稳定光源条件下使用；仅覆盖本次参考反射率范围，不延伸到便携仪或其他光源。',status='approved',approved_by='admin@ciguang.demo'))
        db.commit()
        _seed_monitoring(db)
        print(json.dumps({m.__tablename__:db.scalar(select(func.count()).select_from(m)) for m in (User,Instrument,PairDataset,PairedReading,ComparisonRun,Qualification,MonitoringProfile,MonitoringBatch)},ensure_ascii=False))

def _seed_monitoring(db):
    """Freeze the stable-light N02 run as a baseline and add a few independent daily batches."""
    from datetime import date,timedelta
    run=db.scalar(select(ComparisonRun).where(ComparisonRun.name=='N02 · 稳定光源配对 / λ=1'))
    if not run or run.status!='completed':return
    user=db.scalar(select(User).where(User.email=='analyst@ciguang.demo'))
    name='N02 · 每日批次漂移监测'
    monitor=db.scalar(select(MonitoringProfile).where(MonitoringProfile.name==name))
    if not monitor:
        create_monitor(db,MonitorInput(name=name,baseline_run_id=run.id,consecutive_limit=3,note='每日独立试件批次，按采样日期顺序与冻结的稳定光源基线比较；缓慢漂移用于演示连续超限告警。'),user)
        monitor=db.scalar(select(MonitoringProfile).where(MonitoringProfile.name==name))
    drift_rng=random.Random(911)
    offsets=[.05,.12,.18,2.45,2.6,2.75]
    start=date(2026,10,28)
    for index,offset in enumerate(offsets):
        sampling=start+timedelta(days=index)
        key=f'D{index+1:02d}'
        if db.scalar(select(MonitoringBatch).where(MonitoringBatch.monitor_id==monitor.id,MonitoringBatch.batch_key==key)):continue
        lines=['sample_code,reference_value,candidate_value']
        for j in range(24):
            x=round(12+drift_rng.random()*70,4)
            y=round(x+monitor.baseline['bias']+offset+drift_rng.gauss(0,.22),4)
            y=min(max(y,.01),99.99)
            lines.append(f'D{index+1}-{j+1:03d},{x},{y}')
        ingest_batch(db,monitor.id,BatchInput(batch_key=key,sampling_date=sampling,source_file='daily-optics-batches.csv',note='独立陶瓷试片每日一批。',csv_text='\n'.join(lines)+'\n'),user)
    while True:
        pending=db.scalar(select(MonitoringEvaluation).where(MonitoringEvaluation.monitor_id==monitor.id,MonitoringEvaluation.status.in_(['queued','running'])).order_by(MonitoringEvaluation.version))
        if not pending:break
        execute_evaluation(db,pending.id)
if __name__=='__main__':seed()

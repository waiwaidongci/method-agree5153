from datetime import date
from pydantic import BaseModel, Field


class MonitorInput(BaseModel):
    name: str = Field(min_length=2, max_length=160)
    baseline_run_id: int
    consecutive_limit: int = Field(default=3, ge=2, le=30)
    note: str = Field(default="", max_length=1000)


class BatchInput(BaseModel):
    batch_key: str = Field(min_length=1, max_length=80)
    sampling_date: date
    source_file: str = Field(default="", max_length=180)
    note: str = Field(default="", max_length=600)
    csv_text: str = Field(min_length=20, max_length=1000000)


class ReplayInput(BaseModel):
    from_date: date | None = None
    reason: str = Field(default="", max_length=300)

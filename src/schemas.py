from datetime import datetime
from typing import Any, Literal, Optional
from pydantic import BaseModel, Field


class ToolResponse(BaseModel):
    """所有工具的統一回傳契約"""
    status: Literal["OK", "PASS", "FAIL", "UNKNOWN", "DENIED", "NOT_FOUND", "ERROR"]
    data: Optional[dict[str, Any]] = None
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    error: Optional[dict[str, str]] = None
    timestamp: str = Field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))


class ScheduleEvent(BaseModel):
    event_id: str
    title: str
    start: str = Field(..., description="ISO-8601 格式，例如 2026-11-20T16:00:00")
    end: str = Field(..., description="ISO-8601 格式，例如 2026-11-20T18:00:00")
    location: str
    fixed: bool = False
    is_focus_work: bool = True
    notes: str = ""


class GetContextInput(BaseModel):
    date_range: str = Field(..., description="查詢日期，例如 2026-11-20")
    query_type: Literal["all", "calendar", "todos", "travel", "rules"] = "all"
    version: Optional[str] = None


class CheckConstraintsInput(BaseModel):
    candidate_slots: list[ScheduleEvent]
    existing_events: list[ScheduleEvent]
    travel_matrix_id: str = "TM-2026-V1"
    max_daily_hours: float = 9.0


class CommitActionInput(BaseModel):
    action_payload: ScheduleEvent
    expected_version: str
    approval_id: Optional[str] = None
    idempotency_key: str

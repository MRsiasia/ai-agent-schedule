"""Read-only finish-to-start dependency forecasting for scheduled tasks."""
from collections import defaultdict, deque
from datetime import datetime, timedelta
from heapq import heapify, heappop, heappush
from pathlib import Path
from typing import Annotated

from pydantic import BaseModel, Field, ValidationError, model_validator

from src.schemas import ToolResponse
from src.tools import AUTHORIZED_USERS, DATA_DIR

MAX_DEPTH = 64


class ImpactRequest(BaseModel):
    task_id: str = Field(min_length=1, max_length=64)
    delay_minutes: Annotated[int, Field(strict=True, ge=0, le=100_800)]
    dag_version: str = Field(min_length=1, max_length=64)
    max_depth: Annotated[int, Field(strict=True, ge=0, le=MAX_DEPTH)] = MAX_DEPTH


class Task(BaseModel):
    id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    start: datetime
    end: datetime
    deadline: datetime | None = None
    predecessors: list[str] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def validate_plan(self):
        for value in (self.start, self.end, self.deadline):
            if value is not None and (
                value.tzinfo is not None or value.second or value.microsecond or not 2000 <= value.year <= 2099
            ):
                raise ValueError("時間須為 2000–2099 年、分鐘精度的台北本地時間")
        if self.end <= self.start:
            raise ValueError("任務結束時間須晚於開始時間")
        if len(set(self.predecessors)) != len(self.predecessors):
            raise ValueError("前置任務 ID 重複")
        return self


class DependencyPlan(BaseModel):
    version: str
    user_id: str
    timezone: str = Field(pattern=r"^Asia/Taipei$")
    tasks: list[Task] = Field(min_length=1, max_length=500)


def _minutes(duration: timedelta) -> int:
    return duration // timedelta(minutes=1)


def trace_task_impact(
    request: ImpactRequest,
    *,
    server_user_id: str,
    data_dir: Path = DATA_DIR,
) -> ToolResponse:
    """Forecast a signed deadline margin for one task and its reachable successors."""
    if server_user_id not in AUTHORIZED_USERS:
        return ToolResponse(status="DENIED", error={"code": "DENIED", "message": "無權讀取任務資料。"})

    try:
        plan = DependencyPlan.model_validate_json((data_dir / "dependencies.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return ToolResponse(status="UNKNOWN", error={"code": "DAG_NOT_FOUND", "message": "缺少任務相依資料。"})
    except (OSError, UnicodeError, ValidationError):
        return ToolResponse(status="ERROR", error={"code": "INVALID_DAG", "message": "任務相依檔無法讀取或格式不符。"})

    if plan.user_id != server_user_id:
        return ToolResponse(status="DENIED", error={"code": "DENIED", "message": "資料擁有者與伺服端身分不符。"})

    evidence = [{"source": "dependencies.json", "version": plan.version, "timezone": plan.timezone}]
    if request.dag_version != plan.version:
        return ToolResponse(
            status="UNKNOWN", evidence=evidence,
            error={"code": "DAG_VERSION_MISMATCH", "message": f"請改用目前版本 {plan.version}。"},
        )

    tasks = {task.id: task for task in plan.tasks}
    if len(tasks) != len(plan.tasks):
        return ToolResponse(
            status="ERROR", evidence=evidence, error={"code": "DUPLICATE_TASK", "message": "任務 ID 重複。"},
        )

    root = request.task_id
    if root not in tasks:
        return ToolResponse(
            status="UNKNOWN", evidence=evidence, error={"code": "UNKNOWN_TASK", "message": f"查無任務 {root}。"},
        )

    successors = defaultdict(list)
    for task in plan.tasks:
        for parent in task.predecessors:
            successors[parent].append(task.id)

    reachable = {root}
    queue = deque([root])
    while queue:
        for child in successors[queue.popleft()]:
            if child not in reachable:
                reachable.add(child)
                queue.append(child)

    missing = sorted({p for node in reachable for p in tasks[node].predecessors if p not in tasks})
    if missing:
        return ToolResponse(
            status="UNKNOWN", evidence=evidence, data={"missing_task_ids": missing},
            error={"code": "UNKNOWN_PREDECESSOR", "message": "請補齊前置任務資料。"},
        )

    indegree = {node: sum(p in reachable for p in tasks[node].predecessors) for node in reachable}
    ready = [node for node, count in indegree.items() if not count]
    heapify(ready)
    ordered = []
    while ready:
        node = heappop(ready)
        ordered.append(node)
        for child in successors[node]:
            indegree[child] -= 1
            if not indegree[child]:
                heappush(ready, child)

    if len(ordered) != len(reachable):
        return ToolResponse(
            status="ERROR", evidence=evidence, data={"unresolved_task_ids": sorted(reachable - set(ordered))},
            error={"code": "CYCLE_DETECTED", "message": "相依圖有循環，請修正連線後再試。"},
        )

    invalid_links = [
        [p, node] for node in ordered for p in sorted(tasks[node].predecessors)
        if tasks[p].end > tasks[node].start
    ]
    if invalid_links:
        return ToolResponse(
            status="UNKNOWN", evidence=evidence, data={"inconsistent_links": invalid_links},
            error={"code": "INCONSISTENT_BASELINE", "message": "原始排程已有前置任務晚於後續任務開工。"},
        )

    finish = {}
    depth = {}
    paths = {}
    root_result = {}
    affected = []
    truncated = []
    missing_deadlines = []
    for node in ordered:
        task = tasks[node]
        local_parents = [p for p in task.predecessors if p in reachable]
        depth[node] = 1 + max((depth[p] for p in local_parents), default=-1)
        if depth[node] > request.max_depth:
            truncated.append(node)
            continue

        parent_bounds = [
            (finish.get(p, tasks[p].end), p) for p in task.predecessors
        ]
        latest_end, controlling = max(
            parent_bounds, key=lambda bound: (bound[0], bound[1]), default=(task.start, None)
        )
        if node == root:
            start = task.start + timedelta(minutes=request.delay_minutes)
            path = [node]
        else:
            start = max(task.start, latest_end)
            path = paths[controlling] + [node] if start > task.start else []

        end = start + (task.end - task.start)
        finish[node] = end
        paths[node] = path
        if node != root and start == task.start:
            continue

        slack = _minutes(task.deadline - end) if task.deadline else None
        if slack is None:
            missing_deadlines.append(node)

        record = {
            "task_id": node,
            "planned_start": task.start.isoformat(timespec="minutes"),
            "planned_end": task.end.isoformat(timespec="minutes"),
            "forecast_start": start.isoformat(timespec="minutes"),
            "forecast_end": end.isoformat(timespec="minutes"),
            "deadline": task.deadline.isoformat(timespec="minutes") if task.deadline else None,
            "baseline_slack_minutes": _minutes(task.deadline - task.end) if task.deadline else None,
            "slack_minutes": slack,
            "delay_minutes": _minutes(start - task.start),
            "deadline_missed": slack < 0 if slack is not None else None,
            "path": path,
            "predecessor_finishes": {p: t.isoformat(timespec="minutes") for t, p in sorted(parent_bounds, key=lambda x: x[1])},
        }
        if node == root:
            root_result = record
        else:
            affected.append(record)

    data = {
        "root": root_result,
        "affected": affected,
        "missed_deadline_ids": [
            task["task_id"] for task in [root_result, *affected] if task["deadline_missed"]
        ],
        "truncated_task_ids": sorted(truncated),
        "missing_deadline_ids": sorted(missing_deadlines),
    }
    if truncated or missing_deadlines:
        return ToolResponse(
            status="UNKNOWN", data=data, evidence=evidence,
            error={"code": "PARTIAL_IMPACT", "message": "分析範圍受限或部分任務缺少期限，請補齊後重試。"},
        )
    return ToolResponse(status="OK", data=data, evidence=evidence)

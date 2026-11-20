import csv
import json
from datetime import datetime
from pathlib import Path
from typing import Optional

from src.db import ScheduleDB
from src.schemas import (
    CheckConstraintsInput,
    CommitActionInput,
    GetContextInput,
    ScheduleEvent,
    ToolResponse,
)

DATA_DIR = Path("data")
AUTHORIZED_USERS = {"u_411385045"}


def _parse_iso(ts: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(ts)
    except ValueError:
        return None


def _load_travel_matrix(data_dir: Path = DATA_DIR) -> dict:
    with open(data_dir / "travel_matrix.json", "r", encoding="utf-8") as f:
        return json.load(f)


def get_schedule_context(
    params: GetContextInput,
    *,
    server_user_id: str,
    data_dir: Path = DATA_DIR,
    db: Optional[ScheduleDB] = None,
) -> ToolResponse:
    """檢索授權之行事曆、待辦任務、通勤矩陣與作息規則"""
    if server_user_id not in AUTHORIZED_USERS:
        return ToolResponse(
            status="DENIED",
            error={"code": "DENIED", "message": f"使用者 {server_user_id} 無權存取此行事曆資源。"},
        )

    result_data = {}
    evidence = []

    if params.query_type in ("all", "calendar"):
        with open(data_dir / "calendar.json", "r", encoding="utf-8") as f:
            cal = json.load(f)
        if cal.get("user_id") != server_user_id:
            return ToolResponse(
                status="DENIED",
                error={"code": "DENIED", "message": "請求的資料擁有者與伺服端驗證身分不符。"},
            )
        current_ver = db.ensure_version(server_user_id, cal["version"]) if db else cal["version"]
        matched_events = [e for e in cal["events"] if e["start"].startswith(params.date_range)]
        result_data["calendar_version"] = current_ver
        result_data["events"] = matched_events
        evidence.append({"source": "calendar.json", "version": current_ver, "matched_count": len(matched_events)})

    if params.query_type in ("all", "todos"):
        todos = []
        with open(data_dir / "todos.csv", "r", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if row["deadline"].startswith(params.date_range) or params.query_type == "todos":
                    todos.append(row)
        result_data["todos"] = todos
        evidence.append({"source": "todos.csv", "matched_count": len(todos)})

    if params.query_type in ("all", "travel"):
        tm = _load_travel_matrix(data_dir)
        result_data["travel_matrix"] = tm
        evidence.append({"source": "travel_matrix.json", "matrix_id": tm["matrix_id"], "version": tm["version"]})

    if params.query_type in ("all", "rules"):
        rules_text = (data_dir / "rules.md").read_text(encoding="utf-8")
        result_data["rules"] = rules_text
        evidence.append({"source": "rules.md", "version": "rules_v1"})

    if params.query_type == "calendar" and not result_data.get("events"):
        return ToolResponse(
            status="NOT_FOUND",
            data=result_data,
            evidence=evidence,
            error={"code": "NOT_FOUND", "message": f"查無 {params.date_range} 的行事曆事件，列為資料缺口。"},
        )

    return ToolResponse(status="OK", data=result_data, evidence=evidence)


def check_schedule_constraints(
    params: CheckConstraintsInput,
    *,
    data_dir: Path = DATA_DIR,
) -> ToolResponse:
    """確定性核算：時間重疊、跨區通勤緩衝 (T_end + T_travel + T_buffer <= T_next_start) 與單日工時上限"""
    tm = _load_travel_matrix(data_dir)
    if tm.get("matrix_id") != params.travel_matrix_id:
        return ToolResponse(
            status="ERROR",
            error={"code": "INVALID_TRAVEL_MATRIX", "message": f"找不到通勤矩陣 {params.travel_matrix_id}"},
        )

    route_map = {(r["from_location"], r["to_location"]): r for r in tm["routes"]}
    all_events: list[tuple[ScheduleEvent, datetime, datetime]] = []

    for ev in params.existing_events + params.candidate_slots:
        st, ed = _parse_iso(ev.start), _parse_iso(ev.end)
        if not st or not ed or ed <= st:
            return ToolResponse(
                status="ERROR",
                error={
                    "code": "INVALID_TIME_FORMAT",
                    "message": f"事件 {ev.event_id} ({ev.title}) 時間格式錯誤或結束時間早於/等於開始時間。",
                },
            )
        all_events.append((ev, st, ed))

    all_events.sort(key=lambda x: x[1])

    violations = []
    calculations = []
    evidence = [{"source": "travel_matrix.json", "matrix_id": tm["matrix_id"], "version": tm["version"]}]

    for i in range(len(all_events) - 1):
        ev1, st1, ed1 = all_events[i]
        ev2, st2, ed2 = all_events[i + 1]
        gap_minutes = int((st2 - ed1).total_seconds() // 60)

        if gap_minutes < 0:
            violations.append(
                {
                    "type": "TIME_OVERLAP",
                    "between": [ev1.event_id, ev2.event_id],
                    "overlap_minutes": abs(gap_minutes),
                    "formula": f"{ev1.end} > {ev2.start} (重疊 {abs(gap_minutes)} 分鐘)",
                }
            )
            continue

        if ev1.location != ev2.location:
            route = route_map.get((ev1.location, ev2.location))
            if not route:
                return ToolResponse(
                    status="UNKNOWN",
                    evidence=evidence,
                    error={
                        "code": "MISSING_TRAVEL_TIME",
                        "message": f"查無「{ev1.location} -> {ev2.location}」之通勤基準時間，停止計算並請使用者補充。",
                    },
                )
            t_travel = route["base_minutes"]
            t_buffer = route["buffer_minutes"]
            required_mins = t_travel + t_buffer
            slack_mins = gap_minutes - required_mins

            calc_record = {
                "rule_id": route["rule_id"],
                "from": ev1.event_id,
                "to": ev2.event_id,
                "formula": f"可用空檔 {gap_minutes}m vs 需求 ({t_travel}m 通勤 + {t_buffer}m 緩衝 = {required_mins}m)",
                "slack_minutes": slack_mins,
                "passed": slack_mins >= 0,
            }
            calculations.append(calc_record)

            if slack_mins < 0:
                violations.append(
                    {
                        "type": "TRAVEL_BUFFER_VIOLATION",
                        "rule_id": route["rule_id"],
                        "between": [ev1.event_id, ev2.event_id],
                        "shortfall_minutes": abs(slack_mins),
                        "formula": f"預計遲到 {abs(slack_mins)} 分鐘（含 {t_buffer} 分鐘緩衝）",
                    }
                )

    total_focus_minutes = sum(
        int((ed - st).total_seconds() // 60) for ev, st, ed in all_events if ev.is_focus_work
    )
    total_focus_hours = round(total_focus_minutes / 60.0, 2)
    overload_hours = round(total_focus_hours - params.max_daily_hours, 2)

    calculations.append(
        {
            "rule_id": "RULE-01",
            "formula": f"單日總專注工時 {total_focus_hours} 小時 vs 上限 {params.max_daily_hours} 小時",
            "overload_hours": max(0.0, overload_hours),
            "passed": overload_hours <= 0,
        }
    )
    if overload_hours > 0:
        violations.append(
            {
                "type": "DAILY_HOURS_OVERLOAD",
                "rule_id": "RULE-01",
                "overload_hours": overload_hours,
                "formula": f"{total_focus_hours}h > {params.max_daily_hours}h (超標 {overload_hours} 小時)",
            }
        )

    final_status = "PASS" if not violations else "FAIL"
    return ToolResponse(
        status=final_status,
        data={
            "passed": final_status == "PASS",
            "total_focus_hours": total_focus_hours,
            "calculations": calculations,
            "violations": violations,
        },
        evidence=evidence,
    )


def commit_schedule_action(
    params: CommitActionInput,
    *,
    server_user_id: str,
    db: ScheduleDB,
) -> ToolResponse:
    """僅於使用者明確核准、版本未變動且未重複提交時，寫入模擬行事曆至 SQLite"""
    if server_user_id not in AUTHORIZED_USERS:
        return ToolResponse(
            status="DENIED",
            error={"code": "DENIED", "message": "無權限執行寫入操作。"},
        )

    if not params.approval_id:
        return ToolResponse(
            status="DENIED",
            error={"code": "APPROVAL_REQUIRED", "message": "缺少使用者核准 ID (approval_id)，嚴禁未核准寫入行事曆。"},
        )

    approval = db.get_approval(params.approval_id)
    if not approval or approval["status"] != "APPROVED" or approval["user_id"] != server_user_id:
        return ToolResponse(
            status="DENIED",
            error={"code": "INVALID_APPROVAL", "message": f"核准紀錄 {params.approval_id} 無效或尚未獲准。"},
        )

    existing_commit = db.get_committed_by_key(params.idempotency_key)
    if existing_commit:
        return ToolResponse(
            status="OK",
            data={
                "event_id": existing_commit["event_id"],
                "write_status": "IDEMPOTENT_REPLAY",
                "message": "偵測到相同 idempotency_key，已略過重複寫入。",
            },
        )

    current_version = db.get_current_version(server_user_id)
    if current_version != params.expected_version:
        return ToolResponse(
            status="FAIL",
            error={
                "code": "VERSION_CONFLICT",
                "message": f"行事曆版本已變動（預期 {params.expected_version}，目前為 {current_version}），寫入已攔截，請重新檢核最新時段。",
            },
        )

    now_str = datetime.now().isoformat(timespec="seconds")
    new_version = f"{current_version}_updated"
    db.save_committed_action(
        idempotency_key=params.idempotency_key,
        event_id=params.action_payload.event_id,
        user_id=server_user_id,
        approval_id=params.approval_id,
        payload=params.action_payload.model_dump(),
        committed_at=now_str,
    )
    db.bump_version(server_user_id, new_version)

    return ToolResponse(
        status="OK",
        data={
            "event_id": params.action_payload.event_id,
            "write_status": "COMMITTED",
            "new_version": new_version,
        },
        evidence=[{"approval_id": params.approval_id, "previous_version": current_version}],
    )

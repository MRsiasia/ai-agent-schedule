from src.db import ScheduleDB
from src.schemas import CheckConstraintsInput, CommitActionInput, GetContextInput, ScheduleEvent
from src.tools import check_schedule_constraints, commit_schedule_action, get_schedule_context


def test_friday_scenario_original_plan_fails(tmp_path):
    db = ScheduleDB(db_path=tmp_path / "test_state.db")
    db.ensure_version("u_411385045", "cal_v1")

    ctx = get_schedule_context(
        GetContextInput(date_range="2026-11-20", query_type="all"),
        server_user_id="u_411385045",
        db=db,
    )
    assert ctx.status == "OK"
    existing = [ScheduleEvent(**e) for e in ctx.data["events"]]

    candidates = [
        ScheduleEvent(
            event_id="REQ-01",
            title="三峽校區專題討論",
            start="2026-11-20T16:00:00",
            end="2026-11-20T18:00:00",
            location="三峽校區",
        ),
        ScheduleEvent(
            event_id="TSK-03",
            title="作業修改",
            start="2026-11-20T22:30:00",
            end="2026-11-21T00:00:00",
            location="台北市區",
        ),
    ]
    res = check_schedule_constraints(
        CheckConstraintsInput(candidate_slots=candidates, existing_events=existing)
    )
    assert res.status == "FAIL"
    assert res.data["total_focus_hours"] == 10.0
    violation_types = {v["type"] for v in res.data["violations"]}
    assert "TRAVEL_BUFFER_VIOLATION" in violation_types
    assert "DAILY_HOURS_OVERLOAD" in violation_types


def test_failure_case_1_missing_travel_time(tmp_path):
    db = ScheduleDB(db_path=tmp_path / "test_state.db")
    existing = [
        ScheduleEvent(
            event_id="CAL-01", title="必修課", start="2026-11-20T13:00:00", end="2026-11-20T16:00:00", location="三峽校區"
        )
    ]
    candidates = [
        ScheduleEvent(
            event_id="REQ-99", title="未知地點面試", start="2026-11-20T17:00:00", end="2026-11-20T18:00:00", location="新竹科學園區"
        )
    ]
    res = check_schedule_constraints(
        CheckConstraintsInput(candidate_slots=candidates, existing_events=existing)
    )
    assert res.status == "UNKNOWN"
    assert res.error["code"] == "MISSING_TRAVEL_TIME"


def test_failure_case_2_unapproved_and_version_conflict(tmp_path):
    db = ScheduleDB(db_path=tmp_path / "test_state.db")
    db.ensure_version("u_411385045", "cal_v1")

    new_event = ScheduleEvent(
        event_id="REQ-02", title="週五縮短版專題討論", start="2026-11-20T16:00:00", end="2026-11-20T17:15:00", location="三峽校區"
    )

    res_unapproved = commit_schedule_action(
        CommitActionInput(action_payload=new_event, expected_version="cal_v1", approval_id=None, idempotency_key="idem_001"),
        server_user_id="u_411385045",
        db=db,
    )
    assert res_unapproved.status == "DENIED"
    assert res_unapproved.error["code"] == "APPROVAL_REQUIRED"

    db.record_approval("APP-001", "u_411385045", expected_version="cal_v1")
    db.bump_version("u_411385045", "cal_v2_external")

    res_conflict = commit_schedule_action(
        CommitActionInput(action_payload=new_event, expected_version="cal_v1", approval_id="APP-001", idempotency_key="idem_002"),
        server_user_id="u_411385045",
        db=db,
    )
    assert res_conflict.status == "FAIL"
    assert res_conflict.error["code"] == "VERSION_CONFLICT"

import json
import sqlite3
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from pydantic import ValidationError

from evaluation.run import compare, load_cases, summarize_impact
from src import agent
from src.agent import create_agent_graph
from src.dependencies import ImpactRequest, trace_task_impact

USER = "u_411385045"


def request(task="DEV", minutes=60, **overrides):
    return ImpactRequest.model_validate({
        "task_id": task, "delay_minutes": minutes, "dag_version": "dag_v1", **overrides,
    })


def fixture_at(tmp_path, modify):
    document = json.loads(Path("data/dependencies.json").read_text(encoding="utf-8"))
    modify(document)
    (tmp_path / "dependencies.json").write_text(json.dumps(document), encoding="utf-8")
    return tmp_path


@pytest.mark.parametrize("case", [case for case in load_cases() if case["group"] == "dependency"], ids=lambda case: case["id"])
def test_structured_workflow_matches_oracle_and_does_not_commit(case, tmp_path, monkeypatch):
    monkeypatch.setenv("SERVER_USER_ID", USER)
    db = tmp_path / "workflow.db"
    graph = create_agent_graph(db, data_dir=Path(case["fixture"]).parent)
    state = graph.invoke({"impact_request": case["request"]}, config={"configurable": {"thread_id": "test"}})
    assert not compare(case["expected"], summarize_impact(state["impact_result"]))
    assert state["tool_calls"] == 1
    assert state.get("llm_calls", 0) == 0
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT count(*) FROM committed_actions").fetchone() == (0,)


def test_planned_buffers_absorb_delay():
    result = trace_task_impact(request(minutes=15), server_user_id=USER)
    assert result.status == "OK"
    assert result.data["affected"] == []
    assert result.data["root"]["slack_minutes"] == 105


def test_deadline_equal_to_finish_is_not_missed():
    result = trace_task_impact(request("READ", 80), server_user_id=USER)
    assert result.status == "OK"
    assert result.data["root"]["slack_minutes"] == 0
    assert not result.data["missed_deadline_ids"]


def test_cutoff_reports_partial_impact():
    result = trace_task_impact(request(max_depth=1), server_user_id=USER)
    assert result.status == "UNKNOWN"
    assert result.error["code"] == "PARTIAL_IMPACT"
    assert result.data["truncated_task_ids"] == ["REPORT"]
    assert [item["task_id"] for item in result.data["affected"]] == ["QA"]


def test_cutoff_never_mistakes_short_edge_for_complete_join(tmp_path):
    def add_shortcut(doc):
        doc["tasks"][2]["predecessors"].append("DEV")

    result = trace_task_impact(request(max_depth=1), server_user_id=USER, data_dir=fixture_at(tmp_path, add_shortcut))
    assert result.status == "UNKNOWN"
    assert result.data["truncated_task_ids"] == ["REPORT"]


def test_missing_deadline_preserves_later_task_forecast(tmp_path):
    def remove_deadline(doc):
        del doc["tasks"][1]["deadline"]

    result = trace_task_impact(request(), server_user_id=USER, data_dir=fixture_at(tmp_path, remove_deadline))
    assert result.status == "UNKNOWN"
    assert result.data["missing_deadline_ids"] == ["QA"]
    assert result.data["affected"][0]["slack_minutes"] is None
    assert result.data["affected"][1]["slack_minutes"] == -20


def test_missing_predecessor_never_guesses(tmp_path):
    def add_missing(doc):
        doc["tasks"][1]["predecessors"].append("MISSING")

    result = trace_task_impact(request(), server_user_id=USER, data_dir=fixture_at(tmp_path, add_missing))
    assert result.status == "UNKNOWN"
    assert result.error["code"] == "UNKNOWN_PREDECESSOR"
    assert result.data["missing_task_ids"] == ["MISSING"]


@pytest.mark.parametrize(("query", "code"), [
    (request(task="MISSING"), "UNKNOWN_TASK"),
    (request(dag_version="stale"), "DAG_VERSION_MISMATCH"),
])
def test_unknown_request(query, code):
    result = trace_task_impact(query, server_user_id=USER)
    assert result.status == "UNKNOWN"
    assert result.error["code"] == code


def test_authorization_before_read(tmp_path):
    result = trace_task_impact(request(), server_user_id="outsider", data_dir=tmp_path)
    assert result.status == "DENIED"
    assert result.data is None
    assert result.evidence == []


def test_foreign_data_owner(tmp_path):
    path = fixture_at(tmp_path, lambda doc: doc.update(user_id="other"))
    result = trace_task_impact(request(), server_user_id=USER, data_dir=path)
    assert result.status == "DENIED"
    assert result.evidence == []


def test_model_tool_returns_evidence_without_another_generation(tmp_path, monkeypatch):
    class ToolSelectingModel:
        def bind_tools(self, tools):
            return self

        def invoke(self, messages):
            assert not any(isinstance(message, ToolMessage) for message in messages)
            return AIMessage(content="", tool_calls=[{
                "name": "tool_trace_task_impact", "id": "forecast",
                "args": {"task_id": "DEV", "delay_minutes": 60, "dag_version": "dag_v1"},
            }])

    monkeypatch.setenv("SERVER_USER_ID", USER)
    monkeypatch.setattr(agent, "build_llm", ToolSelectingModel)
    graph = create_agent_graph(tmp_path / "natural.db")
    state = graph.invoke(
        {"messages": [HumanMessage(content="追蹤 DEV 延誤 60 分鐘的影響。")]},
        config={"configurable": {"thread_id": "natural"}},
    )
    assert state["llm_calls"] == state["tool_calls"] == 1
    assert json.loads(state["messages"][-1].content)["data"]["missed_deadline_ids"] == ["QA", "REPORT"]


@pytest.mark.parametrize("invalid_delay", [-1, 100_801, 1.5, True])
def test_delay_is_bounded_integer(invalid_delay):
    with pytest.raises(ValidationError):
        request(minutes=invalid_delay)

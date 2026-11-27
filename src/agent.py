import json
import os
import sqlite3
import uuid
from pathlib import Path
from typing import Annotated, Any, Literal, Optional, TypedDict

from dotenv import load_dotenv
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.types import interrupt

from src.db import DEFAULT_DB_PATH, ScheduleDB
from src.dependencies import MAX_DEPTH, ImpactRequest, trace_task_impact
from src.schemas import CheckConstraintsInput, CommitActionInput, GetContextInput, ScheduleEvent
from src.tools import DATA_DIR, check_schedule_constraints, commit_schedule_action, get_schedule_context

load_dotenv()

MAX_LLM_CALLS = 6
MAX_TOOL_CALLS = 8


class AgentState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]
    user_id: str
    llm_calls: int
    tool_calls: int
    calendar_version: Optional[str]
    pending_commit: Optional[dict]
    approval_decision: Optional[str]
    impact_request: Optional[dict]
    impact_result: Optional[dict]
    impact_observed: bool


def build_llm() -> ChatOpenAI:
    return ChatOpenAI(
        base_url=os.getenv("LLM_BASE_URL", "http://localhost:11434/v1"),
        api_key=os.getenv("LLM_API_KEY", "ollama"),
        model=os.getenv("LLM_MODEL", "qwen2.5:1.5b"),
        temperature=0.0,
        # Ollama's OpenAI-compatible endpoint uses max_tokens.
        extra_body={"max_tokens": 384},
        timeout=90,
        max_retries=0,
    )


def _normalize_date_range(val: Any) -> str:
    if isinstance(val, str):
        return val
    if isinstance(val, dict):
        for k in ("start", "date", "end"):
            if isinstance(val.get(k), str) and val[k]:
                return val[k][:10]
    return json.dumps(val, ensure_ascii=False)


def _normalize_query_type(val: Any) -> str:
    valid = {"all", "calendar", "todos", "travel", "rules"}
    if isinstance(val, str) and val in valid:
        return val
    return "all"


def create_agent_graph(db_path=DEFAULT_DB_PATH, *, data_dir: Path = DATA_DIR, dependency_enabled: bool = True):
    db = ScheduleDB(db_path=db_path)
    server_user_id = os.getenv("SERVER_USER_ID", "u_411385045")

    @tool
    def tool_get_schedule_context(date_range: str, query_type: str = "all") -> str:
        """查詢指定日期的行事曆事件、待辦任務、通勤時間矩陣與作息規則。date_range 必須為日期字串如 '2026-11-20'，query_type 必須為 'all'、'calendar'、'todos'、'travel' 或 'rules' 之一。"""
        norm_date = _normalize_date_range(date_range)
        norm_qtype = _normalize_query_type(query_type)
        resp = get_schedule_context(
            GetContextInput(date_range=norm_date, query_type=norm_qtype),  # type: ignore[arg-type]
            server_user_id=server_user_id,
            db=db,
            data_dir=data_dir,
        )
        return resp.model_dump_json(ensure_ascii=False)

    @tool
    def tool_check_schedule_constraints(
        candidate_slots_json: str,
        existing_events_json: str,
        travel_matrix_id: str = "TM-2026-V1",
        max_daily_hours: float = 9.0,
    ) -> str:
        """確定性檢核候選時段與既有行程是否重疊、跨區通勤加緩衝是否足夠，以及單日專注工時是否超標。兩個參數皆需傳入 JSON 字串。"""
        try:
            cand_raw = json.loads(candidate_slots_json) if isinstance(candidate_slots_json, str) else candidate_slots_json
            exist_raw = json.loads(existing_events_json) if isinstance(existing_events_json, str) else existing_events_json
            cand = [ScheduleEvent(**x) for x in cand_raw]
            exist = [ScheduleEvent(**x) for x in exist_raw]
        except Exception as exc:
            return json.dumps({"status": "ERROR", "error": {"code": "BAD_JSON", "message": str(exc)}}, ensure_ascii=False)

        resp = check_schedule_constraints(
            CheckConstraintsInput(
                candidate_slots=cand,
                existing_events=exist,
                travel_matrix_id=str(travel_matrix_id),
                max_daily_hours=float(max_daily_hours),
            ),
            data_dir=data_dir,
        )
        return resp.model_dump_json(ensure_ascii=False)

    @tool
    def tool_propose_schedule_commit(
        event_id: str,
        title: str,
        start: str,
        end: str,
        location: str,
        expected_version: str,
        notes: str = "",
    ) -> str:
        """提出新增行事曆行程的寫入請求（需經人工審核後才會實際寫入資料庫）。所有欄位皆為字串。"""
        payload = {
            "event_id": str(event_id),
            "title": str(title),
            "start": str(start),
            "end": str(end),
            "location": str(location),
            "fixed": False,
            "is_focus_work": True,
            "notes": str(notes),
            "expected_version": str(expected_version),
        }
        return json.dumps({"status": "PENDING_APPROVAL", "proposed_event": payload}, ensure_ascii=False)

    @tool
    def tool_trace_task_impact(
        task_id: str, delay_minutes: int, dag_version: str, max_depth: int = MAX_DEPTH
    ) -> str:
        """唯讀追蹤任務延誤。輸入任務 ID、延後分鐘、資料版本（示範 dag_v1），可選最大深度。回傳受影響任務、路徑、死線裕量與錯誤代碼。"""
        return trace_task_impact(
            ImpactRequest(task_id=task_id, delay_minutes=delay_minutes, dag_version=dag_version, max_depth=max_depth),
            server_user_id=server_user_id,
            data_dir=data_dir,
        ).model_dump_json(ensure_ascii=False)

    tools = [tool_get_schedule_context, tool_check_schedule_constraints, tool_propose_schedule_commit]
    if dependency_enabled:
        tools.append(tool_trace_task_impact)
    tool_map = {t.name: t for t in tools}
    llm_with_tools = build_llm().bind_tools(tools)

    scheduling_instructions = (
        "你是個人行程與多目標排程規劃 Agent。"
        "呼叫工具時請嚴格遵守各參數型別，字串參數請傳純字串。"
        "所有時間重疊、跨區通勤加緩衝與單日工時上限（預設 9 小時）都必須呼叫 tool_check_schedule_constraints 核算，"
        "查無通勤時間時嚴禁猜測或補 0，必須明確指出資料缺口並提出補問或替代方案。"
        "輸出時請清楚列出：可行性判斷、硬性限制檢核、風險與替代方案、待確認問題。"
    )
    impact_instruction = (
        "若詢問任務延誤、相依性或死線裕量，請呼叫 tool_trace_task_impact 並以其結果為準。"
        "參數缺漏時追問，不要捏造任務 ID 或分鐘數。延誤分析是唯讀作業。"
        if dependency_enabled else ""
    )
    system_prompt = SystemMessage(content=scheduling_instructions + impact_instruction)

    def impact_node(state: AgentState) -> dict:
        result = trace_task_impact(
            ImpactRequest.model_validate(state["impact_request"]),
            server_user_id=server_user_id,
            data_dir=data_dir,
        )
        return {"impact_result": result.model_dump(mode="json"), "tool_calls": 1}

    def agent_node(state: AgentState) -> dict:
        if state.get("llm_calls", 0) >= MAX_LLM_CALLS:
            return {
                "messages": [AIMessage(content="已達單次規劃 LLM 呼叫上限（6 次），請依目前檢核結果確認或補充條件。")]
            }
        response = llm_with_tools.invoke([system_prompt] + state["messages"])
        return {
            "messages": [response],
            "llm_calls": state.get("llm_calls", 0) + 1,
        }

    def tools_node(state: AgentState) -> dict:
        last_msg = state["messages"][-1]
        tool_messages: list[ToolMessage] = []
        pending_commit = state.get("pending_commit")
        calls_used = state.get("tool_calls", 0)
        impact_observed = False

        for tc in getattr(last_msg, "tool_calls", []) or []:
            if calls_used >= MAX_TOOL_CALLS:
                tool_messages.append(
                    ToolMessage(
                        tool_call_id=tc["id"],
                        content=json.dumps({"status": "ERROR", "error": {"code": "MAX_TOOL_CALLS", "message": "已達工具呼叫上限。"}}, ensure_ascii=False),
                    )
                )
                continue

            calls_used += 1
            fn = tool_map.get(tc["name"])
            if not fn:
                tool_messages.append(
                    ToolMessage(
                        tool_call_id=tc["id"],
                        content=json.dumps({"status": "ERROR", "error": {"code": "UNKNOWN_TOOL", "message": f"未知工具 {tc['name']}"}}, ensure_ascii=False),
                    )
                )
                continue

            try:
                raw_out = fn.invoke(tc["args"])
                out_str = str(raw_out)
                if tc["name"] == tool_trace_task_impact.name:
                    impact_observed = True
            except Exception as exc:
                out_str = json.dumps(
                    {"status": "ERROR", "error": {"code": "TOOL_ARGS_ERROR", "message": f"工具參數或執行錯誤：{exc}"}},
                    ensure_ascii=False,
                )

            tool_messages.append(ToolMessage(tool_call_id=tc["id"], content=out_str))

            if tc["name"] == "tool_propose_schedule_commit":
                try:
                    parsed = json.loads(out_str)
                    if parsed.get("status") == "PENDING_APPROVAL":
                        pending_commit = parsed["proposed_event"]
                except Exception:
                    pass

        return {
            "messages": tool_messages,
            "tool_calls": calls_used,
            "pending_commit": None if impact_observed else pending_commit,
            "impact_observed": impact_observed,
        }

    def human_review_node(state: AgentState) -> dict:
        proposal = state.get("pending_commit")
        decision = interrupt(
            {
                "message": "偵測到排程寫入請求，請確認是否核准寫入模擬行事曆？",
                "proposed_event": proposal,
            }
        )
        return {"approval_decision": decision}

    def execute_commit_node(state: AgentState) -> dict:
        user_id = state["user_id"]
        proposal = state.get("pending_commit")
        decision = (state.get("approval_decision") or "").strip().lower()

        if decision != "y" or not proposal:
            msg = "使用者已拒絕寫入行程，未變更模擬行事曆。"
            return {"messages": [AIMessage(content=msg)], "pending_commit": None}

        approval_id = f"APP-{uuid.uuid4().hex[:8]}"
        exp_ver = proposal.get("expected_version") or (db.get_current_version(user_id) or "cal_v1")
        db.record_approval(approval_id=approval_id, user_id=user_id, expected_version=exp_ver, status="APPROVED")

        ev = ScheduleEvent(
            event_id=proposal["event_id"],
            title=proposal["title"],
            start=proposal["start"],
            end=proposal["end"],
            location=proposal["location"],
            fixed=False,
            is_focus_work=True,
            notes=proposal.get("notes", ""),
        )
        resp = commit_schedule_action(
            CommitActionInput(
                action_payload=ev,
                expected_version=exp_ver,
                approval_id=approval_id,
                idempotency_key=f"idem-{proposal['event_id']}-{exp_ver}",
            ),
            server_user_id=user_id,
            db=db,
        )
        return {
            "messages": [AIMessage(content=f"寫入執行結果：{resp.model_dump_json(ensure_ascii=False)}")],
            "pending_commit": None,
        }

    def route_after_agent(state: AgentState) -> Literal["tools", "end"]:
        last_msg = state["messages"][-1]
        if getattr(last_msg, "tool_calls", None):
            return "tools"
        return "end"

    def route_after_tools(state: AgentState) -> Literal["human_review", "agent", "end"]:
        if state.get("impact_observed"):
            return "end"
        if state.get("pending_commit"):
            return "human_review"
        return "agent"

    graph = StateGraph(AgentState)
    graph.add_node("agent", agent_node)
    graph.add_node("tools", tools_node)
    graph.add_node("human_review", human_review_node)
    graph.add_node("execute_commit", execute_commit_node)

    if dependency_enabled:
        # Structured requests enter the same graph with an explicit intent.
        graph.add_node("impact", impact_node)
        graph.add_conditional_edges(
            START, lambda state: "impact" if state.get("impact_request") else "agent",
            {"impact": "impact", "agent": "agent"},
        )
        graph.add_edge("impact", END)
    else:
        graph.add_edge(START, "agent")
    graph.add_conditional_edges("agent", route_after_agent, {"tools": "tools", "end": END})
    graph.add_conditional_edges("tools", route_after_tools, {"human_review": "human_review", "agent": "agent", "end": END})
    graph.add_edge("human_review", "execute_commit")
    graph.add_edge("execute_commit", "agent")

    conn = sqlite3.connect(str(db.db_path), check_same_thread=False)
    checkpointer = SqliteSaver(conn)
    return graph.compile(checkpointer=checkpointer)

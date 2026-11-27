import argparse
import json
import os
import uuid
from dotenv import load_dotenv
from langchain_core.messages import HumanMessage
from langgraph.types import Command
from pydantic import ValidationError

from src.agent import create_agent_graph
from src.dependencies import MAX_DEPTH, ImpactRequest

load_dotenv()

DEFAULT_PROMPT = (
    "今天是 2026-11-20。我今天下午想在三峽校區跟端佑討論專題 2 小時（16:00-18:00），"
    "晚上 19:00 前要趕到台北市區排班，另外今天 23:59 前要交一份 90 分鐘的作業修改，"
    "能否排進今天的行程？有哪些衝突風險與替代方案？"
)


def main() -> None:
    parser = argparse.ArgumentParser(description="個人行程與多目標排程規劃 Agent")
    parser.add_argument("--prompt", help="直接提供一般排程需求")
    commands = parser.add_subparsers(dest="command")
    impact = commands.add_parser("impact", help="以任務 ID 分析延誤傳播，唯讀")
    impact.add_argument("--task", required=True)
    impact.add_argument("--delay", required=True, type=int, help="順延分鐘數")
    impact.add_argument("--version", default="dag_v1")
    impact.add_argument("--max-depth", type=int, default=MAX_DEPTH)
    args = parser.parse_args()
    if args.command == "impact":
        try:
            request = ImpactRequest(
                task_id=args.task, delay_minutes=args.delay, dag_version=args.version,
                max_depth=args.max_depth,
            )
        except ValidationError as exc:
            parser.error(str(exc))

    app = create_agent_graph()
    thread_id = f"run-{uuid.uuid4().hex[:8]}"
    config = {"configurable": {"thread_id": thread_id}, "recursion_limit": 20}
    user_id = os.getenv("SERVER_USER_ID", "u_411385045")

    if args.command == "impact":
        state = app.invoke({"impact_request": request.model_dump()}, config=config)
        print(json.dumps(state["impact_result"], ensure_ascii=False, indent=2))
        return

    print(f"=== 啟動排程 Agent (thread_id={thread_id}) ===")
    user_input = args.prompt
    if user_input is None:
        user_input = input("請輸入排程需求（直接按 Enter 使用預設週五情境）：\n> ").strip()
    if not user_input:
        user_input = DEFAULT_PROMPT

    state_input = {
        "messages": [HumanMessage(content=user_input)],
        "user_id": user_id,
        "llm_calls": 0,
        "tool_calls": 0,
        "calendar_version": None,
        "pending_commit": None,
        "approval_decision": None,
    }

    while True:
        interrupted = False
        for event in app.stream(state_input, config=config, stream_mode="updates"):
            for node_name, payload in event.items():
                if node_name == "__interrupt__":
                    interrupted = True
                    info = payload[0].value if payload else {}
                    print("\n--- [人工審核暫停 HITL] ---")
                    print(info.get("message", "請確認是否核准寫入"))
                    print("擬寫入內容：", info.get("proposed_event"))
                    ans = input("輸入 y 核准寫入，或輸入 n 拒絕：\n> ").strip().lower()
                    state_input = Command(resume=ans)
                elif isinstance(payload, dict) and payload.get("messages"):
                    for message in payload["messages"]:
                        if getattr(message, "tool_calls", None):
                            print(f"[{node_name}] 呼叫工具：{[c['name'] for c in message.tool_calls]}")
                        elif message.content:
                            print(f"\n[{node_name}] 回應：\n{message.content}")

        if not interrupted:
            break


if __name__ == "__main__":
    main()

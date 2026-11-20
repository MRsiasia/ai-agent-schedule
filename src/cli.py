import os
import uuid
from dotenv import load_dotenv
from langchain_core.messages import HumanMessage
from langgraph.types import Command

from src.agent import create_agent_graph

load_dotenv()

DEFAULT_PROMPT = (
    "今天是 2026-11-20。我今天下午想在三峽校區跟端佑討論專題 2 小時（16:00-18:00），"
    "晚上 19:00 前要趕到台北市區排班，另外今天 23:59 前要交一份 90 分鐘的作業修改，"
    "能否排進今天的行程？有哪些衝突風險與替代方案？"
)


def main() -> None:
    app = create_agent_graph()
    thread_id = f"run-{uuid.uuid4().hex[:8]}"
    config = {"configurable": {"thread_id": thread_id}, "recursion_limit": 20}
    user_id = os.getenv("SERVER_USER_ID", "u_411385045")

    print(f"=== 啟動排程 Agent (thread_id={thread_id}) ===")
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
                    last_m = payload["messages"][-1]
                    content = getattr(last_m, "content", "")
                    tcalls = getattr(last_m, "tool_calls", None)
                    if tcalls:
                        print(f"[{node_name}] 呼叫工具：{[c['name'] for c in tcalls]}")
                    elif content:
                        print(f"\n[{node_name}] 回應：\n{content}")

        if not interrupted:
            break


if __name__ == "__main__":
    main()

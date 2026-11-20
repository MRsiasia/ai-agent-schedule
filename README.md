# 智慧型個人行程與多目標排程規劃輔助系統（指定作業二）

本專案為 AI Agent 課程指定作業二之可執行雛形系統，延續指定作業一規劃，採用 LangGraph 狀態機串接確定性 Python 工具，並以 Podman 容器封裝執行。

## 系統架構與機制
- **Agent 邏輯與工作流**：以 LangGraph `StateGraph` 實作有界 ReAct 循環（單次上限 6 次 LLM 呼叫、8 次 Tool 呼叫），搭配 SQLite `SqliteSaver` 保存狀態與 `run_id`。
- **人工審核（HITL）**：涉及排程寫入時透過 `interrupt` 暫停等待使用者確認（`y`/`n`），並以冪等鍵（`idempotency_key`）與版本比對防止重複寫入或版本衝突。
- **容器化部署**：除 LLM Server 外，所有應用程式碼、工具與資料掛載皆於 Podman 容器內執行。

## Tool 定義與 Error Handling
1. **`tool_get_schedule_context`（唯讀）**
   - **目的**：查詢指定日期的行事曆事件、待辦任務、通勤時間矩陣與作息規則。
   - **輸入**：`date_range`（日期字串）、`query_type`（`all` / `calendar` / `todos` / `travel` / `rules`）。
   - **輸出**：JSON 格式回傳 `status`、`data`、`evidence`、`error` 與 `timestamp`。
   - **Error Handling**：身分或資源不符回傳 `DENIED`；查無指定日期事件回傳 `NOT_FOUND` 並列為資料缺口。
2. **`tool_check_schedule_constraints`（唯讀）**
   - **目的**：確定性核算時段重疊、跨區通勤加緩衝（`T_end + T_travel + T_buffer <= T_next_start`）與單日高專注工時上限。
   - **輸入**：`candidate_slots_json`、`existing_events_json`、`travel_matrix_id`、`max_daily_hours`。
   - **輸出**：回傳 `PASS` / `FAIL` / `UNKNOWN`、逐項計算公式與違規項目。
   - **Error Handling**：時間格式或起訖順序錯誤回傳 `INVALID_TIME_FORMAT`；查無兩地通勤基準時間立即停止並回傳 `MISSING_TRAVEL_TIME`，不補 0。
3. **`tool_propose_schedule_commit` / `commit_schedule_action`（受控寫入）**
   - **目的**：提出新增行事曆行程請求，經人工核准後寫入 SQLite 並更新版本號。
   - **輸入**：`event_id`、`title`、`start`、`end`、`location`、`expected_version`、`approval_id`、`idempotency_key`。
   - **輸出**：回傳 `PENDING_APPROVAL` 或寫入狀態與新版本號。
   - **Error Handling**：缺少或無效核准回傳 `DENIED`；版本號不一致攔截並回傳 `VERSION_CONFLICT`；重複 `idempotency_key` 採冪等略過。

## 執行追蹤（Execution Trace）摘要
執行 `python -m src.cli` 輸入週五情境後，Agent 呼叫 `tool_get_schedule_context` 取得 `CAL-01`（13:00-16:00 三峽校區必修課）、`CAL-02`（19:00-22:30 台北市區密室逃脫晚班）、`TSK-03`（90 分鐘作業修改，截止 23:59）及 `LOC-02`（三峽至台北尖峰 75 分鐘 + 緩衝 15 分鐘 = 90 分鐘），進而產出衝突風險分析與替代方案建議。

## 失敗情境（Failure Cases）與處理
1. **缺少通勤基準時間（`MISSING_TRAVEL_TIME`）**：當候選行程前往未建立路線的地點時，工具回傳 `UNKNOWN` 與 `MISSING_TRAVEL_TIME`，停止無效推算並要求補充交通時間。
2. **未核准寫入與行事曆版本衝突（`APPROVAL_REQUIRED` / `VERSION_CONFLICT`）**：未附核准 ID 時拒絕寫入；若核准後行事曆版本已變動，攔截寫入並提示重新檢核最新時段。

## Podman 建置與執行方式
1. 複製環境設定檔並依實際 LLM 端點調整（容器連本機 Ollama 可用 `http://host.containers.internal:11434/v1`）：
   `cp .env.example .env`
2. 建置映像檔：
   `podman build -t schedule-agent:hw2 .`
3. 啟動 CLI（唯讀掛載 `./data`、讀寫掛載 `./storage`）：
   `podman run -it --rm --env-file .env -v ./data:/app/data:ro -v ./storage:/app/storage:rw schedule-agent:hw2`
4. 執行單元測試：
   `podman run --rm --env-file .env -v ./data:/app/data:ro -v ./storage:/app/storage:rw schedule-agent:hw2 python -m pytest -v`

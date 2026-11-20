# 智慧型個人行程與多目標排程規劃輔助系統（指定作業二）

本專案為 AI Agent 課程指定作業二之可執行雛形系統，延續指定作業一規劃，採用 LangGraph 狀態機串接確定性 Python 工具，並以 Podman 容器封裝執行。

## 系統架構與機制
- **Agent 邏輯與工作流**：以 LangGraph `StateGraph` 實作有界 ReAct 循環（單次上限 6 次 LLM 呼叫、8 次 Tool 呼叫），搭配 SQLite `SqliteSaver` 保存狀態與執行紀錄。
- **人工審核（HITL）**：涉及排程寫入時透過 `interrupt` 暫停等待使用者確認（`y`/`n`），並搭配版本比對與冪等鍵（`idempotency_key`）避免重複寫入或版本衝突。
- **容器化部署**：除 LLM Server 外，所有應用程式碼、工具與資料掛載皆於 Podman 容器內執行。

## Tool 定義與 Error Handling
1. **`tool_get_schedule_context`（唯讀）**
   - **目的**：查詢指定日期的行事曆事件、待辦任務、通勤時間矩陣與作息規則。
   - **輸入**：`date_range`（日期字串）、`query_type`（`all` / `calendar` / `todos` / `travel` / `rules`）。
   - **輸出**：JSON 格式回傳 `status`、`data`、`evidence`、`error` 與 `timestamp`。
   - **Error Handling**：身分或資源不符回傳 `DENIED`；查無指定日期資料回傳 `NOT_FOUND` 並列為資料缺口。
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

## Agent 執行追蹤（Execution Trace）
- **使用者輸入（User Input）**：輸入 2026-11-20 週五排程需求（三峽校區討論專題 16:00-18:00、19:00 前趕到台北市區排班、23:59 前完成 90 分鐘作業修改）。
- **Agent 決策（Decision）**：判斷需先掌握當日既有行程、待辦任務、跨區通勤時間與作息規則，決定呼叫 `tool_get_schedule_context`。
- **Tool Call**：呼叫 `tool_get_schedule_context(date_range="2026-11-20", query_type="all")`。
- **執行結果觀察（Observation）**：取得 `CAL-01`（13:00-16:00 三峽校區必修課）、`CAL-02`（19:00-22:30 台北市區密室逃脫晚班）、`TSK-03`（90 分鐘作業修改，截止 23:59）及 `LOC-02`（三峽至台北尖峰 75 分鐘 + 緩衝 15 分鐘 = 90 分鐘）。
- **Agent 決策（Decision）**：彙整時間與通勤限制並評估衝突風險與替代方案；若需寫入行事曆則進入 `tool_propose_schedule_commit` 與人工審核（HITL）確認。
- **執行最後結果**：輸出行程概況、硬性限制檢核、衝突風險、替代方案與待確認問題（詳見 `logs/pytest_result.txt` 與測試案例）。

## 失敗情境（Failure Cases）
### 情境一：缺少通勤基準時間（`MISSING_TRAVEL_TIME`）
- **失敗狀況**：候選行程前往通勤矩陣中未定義路線的地點時，無法完成跨區通勤時間核算。
- **原因**：查詢 `travel_matrix` 找不到對應起訖地點路線，若自行補 0 會導致實際通勤時間嚴重低估。
- **解決方法**：工具回傳 `UNKNOWN` 與 `MISSING_TRAVEL_TIME`，中止無效推算並向使用者提示缺少交通時間，待補充後再重新檢核。

### 情境二：未核准寫入或行事曆版本衝突（`APPROVAL_REQUIRED` / `VERSION_CONFLICT`）
- **失敗狀況**：排程寫入請求被拒絕，或在等待人工審核期間行事曆版本已發生變更。
- **原因**：寫入操作缺少有效核准憑證，或 `expected_version` 與目前資料庫中的行事曆版本不一致。
- **解決方法**：寫入前先經 HITL 人工確認並檢查版本號與 `idempotency_key`；若版本衝突則攔截寫入，要求重新讀取最新行事曆後再評估。

## Podman 建置與執行方式
1. 建立環境設定檔（容器內連線本機 Ollama 可使用 `http://host.containers.internal:11434/v1`）：
   `cp .env.example .env`
2. 建置映像檔：
   `podman build -t schedule-agent:hw2 .`
3. 啟動 CLI（唯讀掛載 `./data`、讀寫掛載 `./storage`）：
   `podman run -it --rm --env-file .env -v ./data:/app/data:ro -v ./storage:/app/storage:rw schedule-agent:hw2`
4. 執行測試：
   `podman run --rm --env-file .env -v ./data:/app/data:ro -v ./storage:/app/storage:rw schedule-agent:hw2 python -m pytest -v`

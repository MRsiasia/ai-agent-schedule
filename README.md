# 智慧型個人行程與多目標排程規劃輔助系統

人工智慧代理人原理與實務 · 指定作業三

作業三延續 [作業二的提交版本](https://github.com/MRsiasia/ai-agent-schedule/tree/2555408a94491a9f40fb4270cc199b6154a37c70)，在既有 LangGraph 排程 Agent 加入唯讀的任務相依分析。使用者提供任務 ID、延後分鐘與資料版本，Agent 會回傳受影響任務、預測起訖、死線裕量、控制路徑與資料證據。

[三頁實驗報告](documents/HW3_Experiment_Report.pdf) · [12 案測試集](evaluation/cases.json) · [原始實驗紀錄](logs/hw3/) · [技術與評分對照](documents/technical-notes.md)

## 用 Podman 重現

需要 Podman 4.9 以上與一般網路連線。Linux 可直接執行。macOS／Windows 若尚未建立 Podman 虛擬機，先執行 `podman machine init` 和 `podman machine start`。已有虛擬機時可用 `podman machine list` 查看狀態。範例使用具多架構支援的 Python 3.11 官方映像與鎖定套件。

```bash
git clone --depth 1 --branch hw3 https://github.com/MRsiasia/ai-agent-schedule.git
cd ai-agent-schedule
podman build --tag schedule-agent:hw3 .
podman run --rm --network=none schedule-agent:hw3 python -m pytest -q
podman run --rm --network=none schedule-agent:hw3 python -m src.cli impact --task DEV --delay 60 --version dag_v1
podman run --rm --network=none schedule-agent:hw3 python -m evaluation.run --repeats 3
```

實際驗證平台與原始輸出見 [Podman 紀錄](logs/hw3/podman-verification.txt)。DEV 範例預期回傳 `status=OK`、`QA` 裕量 −25 分、`REPORT` 裕量 −20 分，與兩條來源路徑。查看截斷結果可加 `--max-depth 1`。內建資料為示範資料，映像內的 `/app/data` 可直接使用。若要保留 SQLite 狀態，執行時加上 `-v schedule-agent-state:/app/storage`。測試產物預設寫入 `/app/storage/evaluation`。需要保留可讀檔案時可命名容器並以 `podman cp <容器名稱>:/app/storage/evaluation ./evaluation-output` 匯出。

## 連接 Ollama

預設模型為 [Qwen2.5-1.5B-Instruct](https://huggingface.co/Qwen/Qwen2.5-1.5B-Instruct)，授權為 Apache 2.0。Ollama 標籤 `qwen2.5:1.5b` 採 Q4_K_M 量化，下載約 1 GB。人工或評估用的 `impact` 指令由 Python 工具核算，無須啟動 LLM。一般自然語言 CLI 與 `--live` 實驗需要 Ollama。

### 使用已安裝的 Ollama

先在 Ollama 主機執行 `ollama pull qwen2.5:1.5b`，並確認 Ollama 應用程式或 `ollama serve` 已啟動。複製 `.env.example` 為 `.env`（PowerShell 可用 `Copy-Item .env.example .env`）。預設 `LLM_BASE_URL` 指向 `http://host.containers.internal:11434/v1`。若 Podman 虛擬機無法連線到 Ollama 的 loopback，依該平台設定 Ollama 監聽於容器可達的主機位址後重啟。也可使用下一節的獨立 Podman 網路。

```bash
podman run --rm -it --env-file .env -v schedule-agent-state:/app/storage schedule-agent:hw3
```

Linux 上，若 Ollama 維持只監聽 `127.0.0.1`，可在上述命令改用 `--network=host -e LLM_BASE_URL=http://127.0.0.1:11434/v1`。這裡的 host 指 Podman 引擎所在的 Linux 主機。

### 在 Podman 網路內啟動 Ollama

這個方式可在 Linux 或有 Podman Machine 的 macOS／Windows 使用。Agent 與 Ollama 在同一個網路，容器之間直接以名稱連線。

```bash
podman network create schedule-hw3
podman run -d --name schedule-ollama --network schedule-hw3 -v schedule-models:/root/.ollama docker.io/ollama/ollama
podman exec schedule-ollama ollama pull qwen2.5:1.5b
podman run --rm -it --network schedule-hw3 -e LLM_BASE_URL=http://schedule-ollama:11434/v1 -v schedule-agent-state:/app/storage schedule-agent:hw3
```

可在一般 CLI 輸入 `請直接呼叫 tool_trace_task_impact。參數為 task_id=DEV、delay_minutes=60、dag_version=dag_v1。`。也可用 `python -m src.cli --prompt "…"` 傳入問題。自然語言工具選擇受模型能力影響。助教驗收可先執行固定參數的 `impact` 指令，直接查看工具證據。[Ollama 實際執行軌跡](logs/hw3/ollama-conversation.txt)

## 重跑實驗與匯出

基本實驗以 12 案各重跑 3 次，每案各執行 Before／After。額外的 `--live` 會在 T07–T10 各組各呼叫同一 Ollama 模型一次，使用相同原始資料，After 會多收到工具觀察。這個測試量測自由生成的轉述，正式 Agent 的相依結果以工具 JSON 為準。

以下假設 Ollama 在 `schedule-hw3` 網路內。若用主機 Ollama，`OLLAMA_URL` 換成對應容器可達位置，位址不帶 `/v1`。

```bash
podman run --name schedule-hw3-eval --network schedule-hw3 -e OLLAMA_URL=http://schedule-ollama:11434 -e LLM_MODEL=qwen2.5:1.5b schedule-agent:hw3 python -m evaluation.run --live --repeats 3
podman cp schedule-hw3-eval:/app/storage/evaluation ./evaluation-output
podman rm schedule-hw3-eval
```

- `scores.csv` 每案、重複次數、版本、判定與延遲。
- `runs.jsonl` 實際工具輸出、欄位差異，以及啟用 `--live` 時的模型原文。
- `summary.json` 總分、分組結果、延遲、模型雜湊與測資雜湊。

## 本機開發

```bash
python -m venv .venv
.venv/bin/python -m pip install -r requirements.lock.txt
.venv/bin/python -m pytest -q
.venv/bin/python -m src.cli impact --task DATA --delay 45
```

PowerShell 可用 `.venv\Scripts\python.exe`。本機 CLI 預設 `LLM_BASE_URL=http://localhost:11434/v1`。若從舊版工作目錄升級，請確認原有 `.env` 的 `LLM_MODEL` 與連線位址。可用 `curl http://127.0.0.1:11434/api/tags` 或 `ollama list` 檢查模型是否已下載。若主要實驗回傳非零狀態，先查 `runs.jsonl` 中的 `mismatches`。

| 設定 | 用途 |
|---|---|
| `LLM_MODEL` | 自然語言與補充實驗模型，預設 `qwen2.5:1.5b` |
| `LLM_BASE_URL` | Agent 用的 Ollama OpenAI 相容端點，包含 `/v1` |
| `OLLAMA_URL` | 補充實驗用的原生 Ollama 端點，無 `/v1` |
| `LLM_API_KEY` | OpenAI 相容客戶端的占位值，預設 `ollama` |
| `SERVER_USER_ID` | 伺服器提供的資料身分，內建示範為 `u_411385045` |

"""Run the paired HW2/HW3 evaluation and save all scores and observations."""
import argparse
import csv
import hashlib
import json
import os
import platform
import statistics
import tempfile
import time
import urllib.request
from datetime import datetime
from pathlib import Path

from src.agent import create_agent_graph
from src.schemas import CheckConstraintsInput, ScheduleEvent
from src.tools import check_schedule_constraints

ROOT = Path(__file__).resolve().parent.parent
CASE_FILE = ROOT / "evaluation/cases.json"
DATA_DIR = ROOT / "data"


def load_cases():
    return json.loads(CASE_FILE.read_text(encoding="utf-8"))


def schedule_observations(case, route_minutes):
    """Use the unchanged HW2 checker. Ranking here is shared evaluation logic."""
    existing = [ScheduleEvent.model_validate(event) for event in case["existing"]]
    results, observations = {}, {}
    for label, option in case["options"].items():
        candidate = [ScheduleEvent.model_validate(event) for event in option]
        result = check_schedule_constraints(
            CheckConstraintsInput(candidate_slots=candidate, existing_events=existing),
            data_dir=DATA_DIR,
        )
        observations[label] = result.model_dump(mode="json")
        data = result.data or {}
        calculations = data.get("calculations", [])
        commute = [item for item in calculations if "slack_minutes" in item]
        slack = [item["slack_minutes"] for item in commute]
        violations = sorted({item["type"] for item in data.get("violations", [])})
        summary = {
            "status": result.status,
            "error": result.error["code"] if result.error else None,
            "violations": violations,
            "focus_hours": data.get("total_focus_hours"),
            "overload_hours": next((item["overload_hours"] for item in calculations if "overload_hours" in item), None),
            "travel_slack": slack,
            "zero_travel_slack": 0 in slack,
        }
        if result.status == "PASS":
            events = sorted(existing + candidate, key=lambda event: datetime.fromisoformat(event.start))
            gaps = [
                int((datetime.fromisoformat(second.start) - datetime.fromisoformat(first.end)).total_seconds() // 60)
                for first, second in zip(events, events[1:]) if first.location == second.location
            ]
            summary["remaining_gap"] = min(gaps + slack, default=0)
            summary["travel_minutes"] = sum(route_minutes[c["rule_id"]] for c in commute)
        results[label] = summary

    feasible = [label for label, result in results.items() if result["status"] == "PASS"]
    status = next(s for s in ("PASS", "UNKNOWN", "ERROR", "FAIL") if any(r["status"] == s for r in results.values()))
    result = {
        "status": status,
        "options": results,
        "best": min(feasible, key=lambda name: (results[name]["travel_minutes"], name), default=None),
        "most_slack": min(feasible, key=lambda name: (-results[name]["remaining_gap"], name), default=None),
        "negotiable_constraints": sorted({v for option in results.values() for v in option["violations"]}) if not feasible else [],
    }
    return result, observations


def summarize_impact(raw):
    data = raw.get("data") or {}
    result = {
        "status": raw["status"],
        "error": (raw.get("error") or {}).get("code"),
        "root_slack": (data.get("root") or {}).get("slack_minutes"),
        "affected": {
            node["task_id"]: {"delay": node["delay_minutes"], "slack": node["slack_minutes"], "path": node["path"]}
            for node in data.get("affected", [])
        },
        "missed": sorted(data.get("missed_deadline_ids", [])),
        "unresolved": data.get("unresolved_task_ids", []),
    }
    return result


def compare(expected, actual, location=""):
    """Return every mismatched leaf. For the affected map, also reject extra IDs."""
    if isinstance(expected, dict) and isinstance(actual, dict):
        differences = [f"{location}.{key}: 額外任務" for key in actual.keys() - expected.keys()] if location == ".affected" else []
        for key, value in expected.items():
            field = f"{location}.{key}"
            if key not in actual:
                differences.append(f"{field}: 缺欄位")
            else:
                differences.extend(compare(value, actual[key], field))
        return differences
    if actual == expected and (isinstance(expected, bool) == isinstance(actual, bool)):
        return []
    return [f"{location}: 預期 {expected!r}，實際 {actual!r}"]


def native_ollama(endpoint, path, payload=None):
    request = urllib.request.Request(
        f"{endpoint.rstrip('/')}/{path}",
        data=json.dumps(payload).encode() if payload else None,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=90) as response:
        return json.load(response)


def model_observation(case, fixture, tool_result, endpoint, model, seed):
    """Same model and base prompt in both runs. Only HW3 receives the tool observation."""
    prompt = (
        f"Question: {case['question']}\nRequest: {json.dumps(case['request'])}\n"
        "Each task row is [id,planned start,planned end,deadline,predecessors]. Finish-to-start edges, fixed durations. "
        "Signed slack = deadline minus forecast finish in minutes. If reachable graph has a cycle, output ERROR, "
        "code CYCLE_DETECTED and unresolved IDs, with empty affected and missed arrays. Otherwise status OK. "
        "Only include downstream tasks whose starts shift. For each affected item provide its controlling path. "
        f"Data: {json.dumps(fixture, ensure_ascii=False, separators=(',', ':'))}\n"
    )
    if tool_result is not None:
        projected = dict(tool_result)
        projected["affected"] = [
            {"id": task, **values} for task, values in tool_result["affected"].items()
        ]
        prompt += "Verified tool observation (reproduce these computed fields faithfully): " + json.dumps(projected, separators=(",", ":"))

    schema = {
        "type": "object",
        "properties": {
            "status": {"type": "string", "enum": ["OK", "ERROR", "UNKNOWN"]},
            "error": {"type": ["string", "null"]},
            "root_slack": {"type": ["integer", "null"]},
            "affected": {"type": "array", "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"}, "delay": {"type": "integer"}, "slack": {"type": "integer"},
                    "path": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["id", "delay", "slack", "path"],
            }},
            "missed": {"type": "array", "items": {"type": "string"}},
            "unresolved": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["status", "error", "root_slack", "affected", "missed", "unresolved"],
    }
    started = time.perf_counter()
    raw, response = None, {}
    try:
        response = native_ollama(endpoint, "api/chat", {
            "model": model, "stream": False, "format": schema, "keep_alive": "15m",
            "messages": [{"role": "user", "content": prompt}],
            "options": {"temperature": 0, "seed": seed, "num_ctx": 4096, "num_predict": 240, "num_thread": 2},
        })
        raw = response["message"]["content"]
        actual = json.loads(raw)
        items = actual["affected"]
        actual["affected"] = {
            item["id"]: {k: v for k, v in item.items() if k != "id"} for item in items
        }
        mismatches = compare(case["expected"], actual)
        if len(items) != len(actual["affected"]):
            mismatches.append("affected: 模型重複輸出任務 ID")
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as failure:
        actual, mismatches = None, [f"{type(failure).__name__}: {failure}"]
    return {
        "llm_calls": 1, "actual": actual, "raw": raw, "mismatches": mismatches,
        "passed": not mismatches, "latency_ms": round((time.perf_counter() - started) * 1000, 3),
        "seed": seed, "done_reason": response.get("done_reason"),
        "prompt_eval_count": response.get("prompt_eval_count"), "eval_count": response.get("eval_count"),
    }


def summarize(records, live):
    summary = {}
    for mode in ("before", "after"):
        rows = [record for record in records if record["mode"] == mode]
        dag = [row for row in rows if row["group"] == "dependency"]
        controls = [row for row in rows if row["group"] == "schedule"]
        latencies = [row["latency_ms"] for row in dag]
        summary[mode] = {
            "passes": sum(row["passed"] for row in rows), "runs": len(rows),
            "dependency_passes": sum(row["passed"] for row in dag), "dependency_runs": len(dag),
            "control_passes": sum(row["passed"] for row in controls), "control_runs": len(controls),
            "latency_ms_dependency": {
                "median": round(statistics.median(latencies), 3), "min": min(latencies), "max": max(latencies),
            },
            "llm_calls": sum(row["llm_calls"] for row in rows),
            "tool_calls": sum(row["tool_calls"] for row in rows),
        }
        if live:
            models = [row["model_result"] for row in dag]
            latencies = [row["latency_ms"] for row in models]
            summary[mode]["model"] = {
                "passes": sum(row["passed"] for row in models), "runs": len(models),
                "median_ms": round(statistics.median(latencies), 3), "min_ms": min(latencies), "max_ms": max(latencies),
                "llm_calls": sum(row["llm_calls"] for row in models),
                "per_case_passes": {
                    case_id: sum(row["model_result"]["passed"] for row in dag if row["case_id"] == case_id)
                    for case_id in sorted({row["case_id"] for row in dag})
                },
            }
    return summary


def run(repeats, out, *, endpoint=None, model=None):
    cases = load_cases()
    travel = json.loads((DATA_DIR / "travel_matrix.json").read_text(encoding="utf-8"))
    route_minutes = {route["rule_id"]: route["base_minutes"] for route in travel["routes"]}
    model_metadata = None
    if endpoint:
        tags = native_ollama(endpoint, "api/tags")
        model_metadata = next((entry for entry in tags["models"] if entry["name"] == model), None)
        if model_metadata is None:
            raise SystemExit(f"Ollama 找不到 {model}，請先 pull。")
        model_metadata = {k: model_metadata[k] for k in ("name", "digest", "details")}
        model_metadata["ollama_version"] = native_ollama(endpoint, "api/version")["version"]
        native_ollama(endpoint, "api/chat", {
            "model": model, "stream": False,
            "messages": [{"role": "user", "content": "Say OK"}],
            "options": {"num_predict": 4, "num_thread": 2, "num_ctx": 4096}, "keep_alive": "15m",
        })

    out.mkdir(parents=True, exist_ok=True)
    records = []
    with tempfile.TemporaryDirectory(prefix="hw3-eval-") as temporary, (out / "runs.jsonl").open("w", encoding="utf-8") as log:
        fixtures = sorted({case["fixture"] for case in cases if case["group"] == "dependency"})
        graphs = {
            name: create_agent_graph(Path(temporary) / f"forecast-{i}.db", data_dir=(ROOT / name).parent)
            for i, name in enumerate(fixtures)
        }
        for repetition in range(1, repeats + 1):
            for index, case in enumerate(cases):
                is_impact = case["group"] == "dependency"
                if is_impact:
                    plan = json.loads((ROOT / case["fixture"]).read_text(encoding="utf-8"))
                    fixture = [
                        [task["id"], task["start"], task["end"], task.get("deadline"), task.get("predecessors", [])]
                        for task in plan["tasks"]
                    ]
                modes = ("before", "after") if (repetition + index) % 2 else ("after", "before")
                for mode in modes:
                    started = time.perf_counter()
                    if not is_impact:
                        actual, observations = schedule_observations(case, route_minutes)
                        tool_calls = len(observations)
                    elif mode == "after":
                        state = graphs[case["fixture"]].invoke(
                            {"impact_request": case["request"]},
                            config={"configurable": {"thread_id": f"{case['id']}-{repetition}"}},
                        )
                        observations = state["impact_result"]
                        actual = summarize_impact(observations)
                        tool_calls = state["tool_calls"]
                    else:
                        observations = None
                        actual = {"status": "UNSUPPORTED"}
                        tool_calls = 0
                    latency = round((time.perf_counter() - started) * 1000, 3)
                    mismatches = compare(case["expected"], actual)
                    record = {
                        "case_id": case["id"], "group": case["group"], "repeat": repetition, "mode": mode,
                        "passed": not mismatches, "latency_ms": latency, "tool_calls": tool_calls, "llm_calls": 0,
                        "actual": actual, "observations": observations, "mismatches": mismatches,
                    }
                    if endpoint and is_impact:
                        record["model_result"] = model_observation(
                            case, fixture, actual if mode == "after" else None, endpoint, model, repetition,
                        )
                        print(f"{case['id']} {mode} {repetition}/{repeats} LLM pass={record['model_result']['passed']}", flush=True)
                    records.append(record)
                    log.write(json.dumps(record, ensure_ascii=False) + "\n")
                    log.flush()

    with (out / "scores.csv").open("w", encoding="utf-8", newline="") as file:
        writer = csv.writer(file, lineterminator="\n")
        writer.writerow(["case_id", "group", "repeat", "mode", "passed", "latency_ms", "tool_calls",
                         "llm_calls", "model_passed", "model_latency_ms"])
        for row in records:
            model_row = row.get("model_result", {})
            writer.writerow([*(row[k] for k in ("case_id", "group", "repeat", "mode", "passed", "latency_ms",
                             "tool_calls", "llm_calls")), model_row.get("passed"), model_row.get("latency_ms")])
    metadata = {
        "dataset_sha256": hashlib.sha256(CASE_FILE.read_bytes()).hexdigest(),
        "input_sha256": {
            name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
            for name in [*fixtures, "data/travel_matrix.json"]
        },
        "platform": platform.platform(), "python": platform.python_version(),
        "model": model_metadata, "repeats": repeats,
        "metrics": summarize(records, bool(endpoint)),
    }
    (out / "summary.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return metadata


if __name__ == "__main__":
    args = argparse.ArgumentParser(description=__doc__)
    args.add_argument("--repeats", type=int, default=3)
    args.add_argument("--output", type=Path, default=Path("storage/evaluation"))
    args.add_argument("--live", action="store_true", help="另外以同一個 Ollama 模型檢驗 T07–T10 的回答")
    args.add_argument("--ollama", default=os.getenv("OLLAMA_URL", "http://127.0.0.1:11434"))
    args.add_argument("--model", default=os.getenv("LLM_MODEL", "qwen2.5:1.5b"))
    options = args.parse_args()
    if options.repeats < 1:
        args.error("--repeats must be at least 1")
    summary = run(options.repeats, options.output, endpoint=options.ollama if options.live else None, model=options.model)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if summary["metrics"]["after"]["passes"] != summary["metrics"]["after"]["runs"]:
        raise SystemExit(1)

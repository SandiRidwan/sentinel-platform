"""One run, three backends, the same spans.

The agent writes spans to `quality/reports/agent_trace.jsonl` as it runs. This module takes
that same span list and sends it to two real backends, then reads the spans back **from each
backend's own store** and compares:

    span count      if they differ, one side dropped data
    span names      if they differ, the two sides disagree about what happened
    attribute keys  if they differ, the payload lost the context that makes a trace useful

Why read back instead of trusting the exporter's return value: an exporter that reports
"9 spans sent" and a backend that stored 3 is exactly the failure this check exists to
catch. Phoenix is read through its REST API (`/v1/projects/<name>/spans`); LangSmith is read
through the local receiver's `/runs/count`.

The LangSmith half runs against `agents/export/receiver.py`, a local implementation of the
ingest contract, because the hosted service needs an account. The payload is produced by
LangSmith's own SDK, so what is proven is that the trace serialises into LangSmith's format
and arrives intact — not that a paid account accepted it. That limit is in the report.

Durations are carried but not compared. The three backends receive the same span list, so a
duration comparison would only measure this machine's clock, and the first version of this
check failed on exactly that (20 ms vs 57 ms between two runs) while nothing was wrong.

    python -m agents.trace_parity
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import duckdb

from agents.eval import build_cases
from agents.export.langsmith import export_spans as export_langsmith
from agents.export.phoenix import export_spans as export_phoenix, read_spans as read_phoenix_spans
from agents.graph import Agent
from agents.tracing import Trace
from tools.common.config import get_settings

REPORT = Path("quality/reports/trace_parity.json")
PHOENIX = os.environ.get("PHOENIX_COLLECTOR_ENDPOINT", "http://localhost:6006")
QUESTIONS = 3

def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])

def http_json(url: str, timeout: float = 5.0) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))

def wait_for(url: str, timeout: float = 30.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            http_json(url, timeout=2.0)
            return True
        except (urllib.error.URLError, OSError, ValueError):
            time.sleep(0.5)
    return False

def ensure_phoenix() -> bool:
    """Phoenix is a separate server; start it if it is not already answering."""
    if wait_for(f"{PHOENIX}/v1/projects", timeout=2):
        return True
    helper = Path(__file__).resolve().parent / "phoenix_up.py"
    subprocess.run([sys.executable, str(helper)], cwd=str(get_settings().root), capture_output=True, text=True)
    return wait_for(f"{PHOENIX}/v1/projects", timeout=60)

def start_receiver() -> tuple[subprocess.Popen, str]:
    port = free_port()
    endpoint = f"http://127.0.0.1:{port}"
    proc = subprocess.Popen(
        [sys.executable, "-m", "agents.export.receiver", "--port", str(port)],
        cwd=str(get_settings().root),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if not wait_for(f"{endpoint}/health", timeout=30):
        proc.terminate()
        out, err = proc.communicate(timeout=5)
        raise RuntimeError(f"langsmith receiver did not start:\n{out}\n{err}")
    return proc, endpoint

def questions(n: int = QUESTIONS) -> list[str]:
    settings = get_settings()
    connection = duckdb.connect(str(settings.duckdb_path), read_only=True)
    try:
        valid, _ = build_cases(connection)
    finally:
        connection.close()
    return [case["question"] for case in valid[:n]]

def summarise(spans: list[dict]) -> dict:
    names: dict[str, int] = {}
    for span in spans:
        names[span["name"]] = names.get(span["name"], 0) + 1
    return {"spans": len(spans), "by_name": dict(sorted(names.items()))}

RESERVED_SPAN_KEYS = {"ts", "name", "duration_ms"}

def attribute_keys(spans: list[dict], prefix: str = "") -> dict[str, list[str]]:
    """Attribute keys per span name, with the exporter's prefix removed.

    Three shapes are handled because the three sides store a span differently: the local
    JSONL keeps attributes at the top level of the span object, Phoenix returns them under
    `attributes`, and the LangSmith receiver stores them under `inputs.attributes`.
    """
    keys: dict[str, set[str]] = {}
    for span in spans:
        attributes = span.get("attributes")
        if attributes is None:
            attributes = (span.get("inputs") or {}).get("attributes")
        if attributes is None:
            attributes = {key: value for key, value in span.items() if key not in RESERVED_SPAN_KEYS}

        stripped = {
            (key[len(prefix):] if prefix and key.startswith(prefix) else key)
            for key in attributes
        }
        keys.setdefault(span["name"], set()).update(stripped - RESERVED_SPAN_KEYS)
    return {name: sorted(values) for name, values in sorted(keys.items())}

def wait_for_spans(project: str, expected: int, timeout: float = 30.0) -> list[dict]:
    """Poll the backend until it reports the expected number of spans.

    Export is asynchronous on both sides, so reading immediately after the call reports a
    partial trace. Polling for the expected count is not a workaround for a race in the
    check: a backend that never reaches the count is the failure being detected.
    """
    deadline = time.time() + timeout
    last: list[dict] = []
    while time.time() < deadline:
        try:
            last = read_phoenix_spans(project, PHOENIX)
        except Exception:  # noqa: BLE001 - the backend may not have the project yet
            last = []
        if len(last) >= expected:
            return last
        time.sleep(1)
    return last

def main() -> int:
    started = datetime.now(timezone.utc)
    project = f"sentinel-parity-{started.strftime('%Y%m%dT%H%M%S')}"
    asked = questions()

    # 1. Local: the run itself. This is the source the other two are compared against.
    trace = Trace(path=Path("quality/reports/agent_trace.jsonl"))
    agent = Agent(trace=trace)
    try:
        [agent.ask(question) for question in asked]
    finally:
        agent.close()

    local = summarise(trace.spans)
    print(f"local trace : {local['spans']} spans {local['by_name']}")

    errors: dict[str, str] = {}

    # 2. Phoenix over OTLP, read back through Phoenix's REST API.
    phoenix: dict | None = None
    if ensure_phoenix():
        try:
            export_phoenix(trace.spans, PHOENIX, project)
            stored = wait_for_spans(project, len(trace.spans))
            phoenix = {
                **summarise(stored),
                "attributes": attribute_keys(stored, prefix="sentinel."),
                "project": project,
            }
            print(f"phoenix     : {phoenix['spans']} spans {phoenix['by_name']} (project {project})")
        except Exception as exc:  # noqa: BLE001 - reported, not raised
            errors["phoenix"] = f"{type(exc).__name__}: {exc}"
            print(f"phoenix     : FAILED {errors['phoenix']}")
    else:
        errors["phoenix"] = "collector did not start"
        print("phoenix     : not reachable")

    # 3. LangSmith wire format, through the SDK, into the local receiver.
    langsmith: dict | None = None
    receiver, endpoint = start_receiver()
    try:
        export_langsmith(trace.spans, endpoint, project)
        counts = http_json(f"{endpoint}/runs/count")
        langsmith = {
            "spans": counts["count"],
            "by_name": counts["by_name"],
            "attributes": counts.get("attributes"),
            "rejected": counts["rejected"],
            "project": project,
            "endpoint": endpoint,
        }
        print(f"langsmith   : {langsmith['spans']} runs {langsmith['by_name']} (project {project})")
    except Exception as exc:  # noqa: BLE001
        errors["langsmith"] = f"{type(exc).__name__}: {exc}"
        print(f"langsmith   : FAILED {errors['langsmith']}")
    finally:
        receiver.terminate()
        try:
            receiver.wait(timeout=5)
        except subprocess.TimeoutExpired:
            receiver.kill()

    # --- comparison ---

    checks: dict[str, bool] = {}
    if phoenix is not None:
        checks["phoenix_span_count_matches"] = phoenix["spans"] == local["spans"]
        checks["phoenix_span_names_match"] = phoenix["by_name"] == local["by_name"]
        checks["phoenix_attributes_match"] = phoenix["attributes"] == attribute_keys(trace.spans)
    if langsmith is not None:
        checks["langsmith_span_count_matches"] = langsmith["spans"] == local["spans"]
        checks["langsmith_span_names_match"] = langsmith["by_name"] == local["by_name"]
        checks["langsmith_attributes_match"] = langsmith["attributes"] == attribute_keys(trace.spans)
        checks["langsmith_accepted_every_run"] = not langsmith["rejected"]

    if errors:
        result = "CANNOT_VERIFY"
    else:
        result = "PASS" if checks and all(checks.values()) else "FAIL"

    report = {
        "ran_at": started.isoformat(),
        "project": project,
        "questions_asked": len(asked),
        "local_trace": {**local, "attributes": attribute_keys(trace.spans), "file": "quality/reports/agent_trace.jsonl"},
        "phoenix": phoenix,
        "langsmith": langsmith,
        "errors": errors,
        "checks": checks,
        "result": result,
        "duration_compared": False,
        "notes": [
            "Phoenix spans are read back from Phoenix's REST API, not from the exporter's return value.",
            "LangSmith runs are read back from a local receiver that implements the ingest contract; "
            "no LangSmith account was used, so the hosted service was not exercised. The payload is "
            "produced by LangSmith's own SDK.",
            "Durations are carried but not compared: all three backends receive the same span list, so a "
            "duration comparison would measure this machine's clock, not the export.",
        ],
    }

    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print()
    for name, ok in checks.items():
        print(f"  [{'OK  ' if ok else 'FAIL'}] {name}")
    print(f"RESULT: {result}")
    return 0 if result == "PASS" else (2 if result == "CANNOT_VERIFY" else 1)

if __name__ == "__main__":
    raise SystemExit(main())

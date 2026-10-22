"""Export the agent's spans to LangSmith's wire format.

The point is not that this repository can use LangSmith. It is that the trace the agent
already produces is not in a proprietary shape: the same spans can be written as OTLP (for
Phoenix, Tempo, Jaeger, a collector) or as LangSmith runs, without changing the agent.

Two honest limits, stated here rather than discovered by a reader:

  * No LangSmith account is used. The payload is produced by LangSmith's own SDK
    (`langsmith.Client`) and posted to a local receiver that validates the fields the
    LangSmith API requires. That proves the payload is well-formed and carries the same
    spans; it does not prove the hosted service accepted it.
  * Durations are copied from the local span, not re-measured. The comparison in
    `agents/trace_parity.py` therefore checks counts and names, and treats duration as
    data to be carried, not as a measurement to be reproduced.

Run ids are derived from the span, not random, so exporting the same trace twice produces
the same ids and a receiver can deduplicate instead of double-counting.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

RUNS_NAMESPACE = uuid.UUID("6f2f1b1e-3c2a-4a5e-9a77-5f4f2c7c1a10")

def _run_id(trace_id: str, index: int, name: str) -> str:
    return str(uuid.uuid5(RUNS_NAMESPACE, f"{trace_id}:{index}:{name}"))

def _timestamp(value: str | None, fallback: datetime) -> datetime:
    if not value:
        return fallback
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return fallback

def to_runs(spans: list[dict], project: str = "sentinel-agent") -> list[dict]:
    """Convert local spans to LangSmith run payloads.

    Kept as a pure function so the conversion can be tested without a server, and so the
    parity check can compare the payload it is about to send with the one that arrived.
    """
    now = datetime.now(timezone.utc)
    trace_id = str(uuid.uuid5(RUNS_NAMESPACE, project + ":" + (spans[0].get("ts") or "") if spans else project))

    runs: list[dict] = []
    for index, span in enumerate(spans):
        started = _timestamp(span.get("ts"), now)
        finished = started + timedelta(milliseconds=float(span.get("duration_ms") or 0))
        attributes = {
            key: value
            for key, value in span.items()
            if key not in {"ts", "name", "duration_ms"}
        }
        runs.append(
            {
                "id": _run_id(trace_id, index, span["name"]),
                "trace_id": trace_id,
                "name": span["name"],
                "run_type": "chain",
                "start_time": started.isoformat(),
                "end_time": finished.isoformat(),
                "inputs": {"attributes": attributes} if attributes else {},
                "outputs": {},
                "extra": {"metadata": {"duration_ms": span.get("duration_ms")}},
                "project_name": project,
                # Dotted order is what LangSmith sorts a trace by; a flat list of spans is
                # still a valid trace, so the order is the start timestamp plus the run id.
                "dotted_order": f"{started.strftime('%Y%m%dT%H%M%S%fZ')}{_run_id(trace_id, index, span['name'])}",
            }
        )
    return runs

def export_spans(spans: list[dict], api_url: str, project: str = "sentinel-agent") -> dict:
    """Post the spans through LangSmith's SDK.

    The SDK is used rather than a hand-written POST so the payload is LangSmith's own
    serialisation, not this repository's reading of the docs. `create_run` is synchronous,
    which is what makes the count checkable immediately after the call.
    """
    from langsmith import Client

    client = Client(
        api_url=api_url,
        api_key=os.environ.get("LANGSMITH_API_KEY", "sentinel-local-receiver"),
        auto_batch_tracing=False,
    )

    runs = to_runs(spans, project)
    for run in runs:
        client.create_run(
            name=run["name"],
            inputs=run["inputs"],
            run_type=run["run_type"],
            project_name=project,
            id=run["id"],
            start_time=run["start_time"],
            end_time=run["end_time"],
            outputs=run["outputs"],
            extra=run["extra"],
            dotted_order=run["dotted_order"],
        )

    return {"exported": len(runs), "project": project, "api_url": api_url, "trace_id": runs[0]["trace_id"] if runs else None}

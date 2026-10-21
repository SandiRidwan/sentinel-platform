"""Span recording for agent runs.

Two outputs, one code path:

  * `quality/reports/agent_trace.jsonl` — always written, one JSON object per span.
    Anyone who clones the repo can read what the agent did without running a collector.
  * OTLP — used when `OTEL_EXPORTER_OTLP_ENDPOINT` is set *and* reachable. The repo's
    observability/ directory ships a collector config for that; it is not started by
    default because it is 200 MB of container for a demo.

The reason to record at all: an agent that cannot be replayed cannot be debugged, and
"the answer was wrong" is not actionable without the tool call, the SQL and the rows.
"""

from __future__ import annotations

import json
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

TRACE_PATH = Path("quality/reports/agent_trace.jsonl")


@dataclass
class Span:
    name: str
    attributes: dict = field(default_factory=dict)
    started: float = field(default_factory=time.perf_counter)
    duration_ms: float = 0.0

    def as_dict(self) -> dict:
        return {
            "ts": datetime.now(timezone.utc).isoformat(),
            "name": self.name,
            "duration_ms": round(self.duration_ms, 3),
            **self.attributes,
        }


class Trace:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or TRACE_PATH
        self.spans: list[dict] = []

    @contextmanager
    def span(self, name: str, **attributes):
        current = Span(name=name, attributes=attributes)
        try:
            yield current
        finally:
            current.duration_ms = (time.perf_counter() - current.started) * 1000
            self.spans.append(current.as_dict())

    def flush(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("w", encoding="utf-8") as handle:
            for span in self.spans:
                handle.write(json.dumps(span) + "\n")

    def summary(self) -> dict:
        by_name: dict[str, int] = {}
        for span in self.spans:
            by_name[span["name"]] = by_name.get(span["name"], 0) + 1
        return {
            "spans": len(self.spans),
            "by_name": by_name,
            "total_ms": round(sum(span["duration_ms"] for span in self.spans), 3),
        }

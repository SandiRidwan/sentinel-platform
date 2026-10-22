"""Export the agent's spans to Phoenix (or any OTLP/HTTP collector).

The transport is the plain OTLP span exporter, not `arize-phoenix-otel`: that package pins
its own OpenTelemetry versions and conflicts with the platform's. Using the wire protocol
directly is also the more honest demonstration — it shows what is actually on the wire, which
is the whole argument for portability.
"""

from __future__ import annotations

import json
import urllib.request

from agents.otel_export import build_tracer_provider

def export_spans(
    spans: list[dict],
    endpoint: str,
    project: str = "sentinel-agent",
    service_name: str = "sentinel-agent",
) -> dict:
    """Re-emit local spans as OTLP spans under a named Phoenix project.

    Each local span becomes one OTLP span with the same name and its attributes copied
    across. The local duration is carried as an attribute rather than faked into the OTLP
    timestamps: the two backends are compared on span identity, and inventing timestamps to
    make a duration comparison pass would be the measurement lying about what it measured.
    """
    provider = build_tracer_provider(endpoint, service_name, project_name=project)
    tracer = provider.get_tracer("agents.export.phoenix")

    for span in spans:
        with tracer.start_as_current_span(span["name"]) as otel_span:
            for key, value in span.items():
                if key in {"ts", "name", "duration_ms"}:
                    continue
                otel_span.set_attribute(f"sentinel.{key}", str(value))
            otel_span.set_attribute("sentinel.duration_ms", float(span.get("duration_ms") or 0))

    provider.force_flush(timeout_millis=10_000)
    provider.shutdown()
    return {"exported": len(spans), "endpoint": endpoint, "project": project}

def read_spans(project: str, endpoint: str, limit: int = 1000) -> list[dict]:
    """Read back what Phoenix actually stored for a project.

    This is the part that makes the parity check real: the exporter's own return value says
    what it tried to send, and Phoenix's store says what arrived.
    """
    url = f"{endpoint.rstrip('/')}/v1/projects/{project}/spans?limit={limit}"
    with urllib.request.urlopen(url, timeout=10) as response:
        payload = json.loads(response.read().decode("utf-8"))
    return payload.get("data") or []

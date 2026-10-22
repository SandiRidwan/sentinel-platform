"""OTLP export for agent traces.

Why this exists rather than importing `arize-phoenix-otel` directly: that package pins its own
OpenTelemetry versions, and this environment already pins a different set for the platform's
own instrumentation. Conflict showed up immediately as
`opentelemetry-semantic-conventions 0.66b1 is incompatible`. The API used here is the plain
OTLP/HTTP span exporter, which every backend in this space accepts: Phoenix, LangSmith's
self-hosted ingest, Jaeger, Tempo, a collector.

The point of the exercise is that the trace format is not proprietary. A thin exporter over
OTLP demonstrates that far better than an SDK wrapper would, because it shows what is actually
on the wire.

    python -m agents.otel_export      # sends one span to the configured endpoint
"""

from __future__ import annotations

import os

from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter


def build_tracer_provider(endpoint: str, service_name: str = "sentinel-agent", project_name: str | None = None) -> TracerProvider:
    """A provider whose spans are exported over OTLP/HTTP.

    `BatchSpanProcessor` plus an explicit `force_flush` in the caller: a script that exits
    without flushing loses the last batch, and the symptom is a trace that is missing one
    or two spans, which looks like an agent bug rather than an export bug.

    `project_name` sets `openinference.project.name`, which is how Phoenix decides which
    project a span belongs to. Without it every run lands in `default` and a parity check
    cannot tell one run's spans from the previous run's.
    """
    attributes = {"service.name": service_name}
    if project_name:
        attributes["openinference.project.name"] = project_name
    provider = TracerProvider(resource=Resource.create(attributes))
    provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{endpoint.rstrip('/')}/v1/traces"))
    )
    return provider


def main() -> int:
    endpoint = os.environ.get("PHOENIX_COLLECTOR_ENDPOINT", "http://localhost:6006")
    provider = build_tracer_provider(endpoint)
    tracer = provider.get_tracer("agents.otel_export")

    with tracer.start_as_current_span("export_probe") as span:
        span.set_attribute("sentinel.probe", "true")

    provider.force_flush(timeout_millis=10_000)
    provider.shutdown()
    print(f"exported one span to {endpoint}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

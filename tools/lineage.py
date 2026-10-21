"""Lineage: emit OpenLineage events and render the graph.

Two artefacts, because they answer different questions:

  events.jsonl   OpenLineage RunEvent records — the interoperable form another tool can
                 ingest. Emitted from the real dbt manifest and the real ingest_run table,
                 not hand-written.
  lineage.mmd    A Mermaid diagram a human can read in the README.

    python -m tools.lineage
    python -m tools.lineage --render-only
"""

from __future__ import annotations

import argparse
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

from tools.common.config import ROOT, get_settings
from tools.common.logging_setup import get_logger, setup_logging

log = get_logger("lineage")

MANIFEST = ROOT / "transform" / "dbt" / "target" / "manifest.json"
OUT_DIR = ROOT / "governance" / "lineage"
EVENTS = OUT_DIR / "events" / "events.jsonl"
GRAPH = OUT_DIR / "lineage.mmd"


def read_manifest() -> dict:
    if not MANIFEST.exists():
        return {}
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def model_graph(manifest: dict) -> list[tuple[str, str]]:
    """Edges (parent -> child) from dbt's own dependency graph.

    Taken from the manifest rather than parsed out of SQL: the manifest is what dbt
    actually compiled, so the graph cannot disagree with what ran.
    """
    edges: list[tuple[str, str]] = []
    for node_id, node in (manifest.get("nodes") or {}).items():
        if node.get("resource_type") not in {"model", "snapshot"}:
            continue
        child = node.get("name", node_id)
        for parent in node.get("depends_on", {}).get("nodes", []):
            parent_node = (manifest.get("nodes") or {}).get(parent, {})
            if parent_node.get("resource_type") == "source":
                edges.append((f"source.{parent_node.get('name', parent)}", child))
            else:
                edges.append((parent_node.get("name", parent), child))
    return edges


def source_edges() -> list[tuple[str, str]]:
    """ingest_run rows give source -> spider, which is the left end of the graph."""
    try:
        import psycopg2

        settings = get_settings()
        with psycopg2.connect(settings.pg.psycopg_dsn) as conn, conn.cursor() as cur:
            cur.execute("select distinct source, spider from governance.ingest_run order by 1")
            return [(f"source.{source}", spider) for source, spider in cur.fetchall()]
    except Exception as exc:  # noqa: BLE001 - lineage must not fail a pipeline
        log.warning("could not read ingest_run: %s", exc)
        return []


def emit_events(edges: list[tuple[str, str]]) -> int:
    """Write one OpenLineage RunEvent per edge.

    A real emitter would post these to a Marquez/OpenLineage endpoint. Writing them as
    JSONL keeps the format honest and the repository runnable offline; swapping the sink is
    one function.
    """
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    EVENTS.parent.mkdir(parents=True, exist_ok=True)

    now = datetime.now(timezone.utc).isoformat()
    written = 0
    with EVENTS.open("w", encoding="utf-8") as handle:
        for parent, child in edges:
            event = {
                "eventType": "COMPLETE",
                "eventTime": now,
                "run": {"runId": str(uuid.uuid4())},
                "job": {"namespace": "sentinel", "name": child},
                "inputs": [{"namespace": "sentinel", "name": parent}],
                "outputs": [{"namespace": "sentinel", "name": child}],
                "producer": "https://github.com/SandiRidwan/sentinel-platform",
                "schemaURL": "https://openlineage.io/spec/1-0-5/OpenLineage.json",
            }
            handle.write(json.dumps(event) + "\n")
            written += 1
    return written


def render_mermaid(edges: list[tuple[str, str]]) -> str:
    lines = ["flowchart LR"]
    seen_nodes: set[str] = set()
    for parent, child in edges:
        for node in (parent, child):
            if node not in seen_nodes:
                safe = node.replace(".", "_").replace(" ", "_")
                if node.startswith("source."):
                    lines.append(f'    {safe}[/"{node}"/]')
                else:
                    lines.append(f'    {safe}["{node}"]')
                seen_nodes.add(node)
        lines.append(f"    {parent.replace('.', '_').replace(' ', '_')} --> {child.replace('.', '_').replace(' ', '_')}")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    setup_logging()
    parser = argparse.ArgumentParser(description="Emit OpenLineage events and render the graph.")
    parser.add_argument("--render-only", action="store_true")
    args = parser.parse_args(argv)

    manifest = read_manifest()
    if not manifest:
        print("  dbt manifest not found; run 'make docs' or 'dbt build' first")
        return 1

    edges = source_edges() + model_graph(manifest)
    print(f"  graph edges: {len(edges)}")

    if not args.render_only:
        count = emit_events(edges)
        print(f"  OpenLineage events: {count} -> {EVENTS.relative_to(ROOT)}")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    GRAPH.write_text(render_mermaid(edges), encoding="utf-8")
    print(f"  mermaid: {GRAPH.relative_to(ROOT)}")

    # A small assertion: a graph with no edges means the manifest was empty or the names
    # did not match, which is a silent failure worth surfacing.
    if not edges:
        print("  WARNING: no edges found — lineage would be empty")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

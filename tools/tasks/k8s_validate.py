"""Validate the Kubernetes manifests offline (no cluster required).

kubectl's client-side dry-run still contacts the API server for discovery, so it cannot
validate a manifest on a machine with no cluster. This parses the YAML and asserts the
structural invariants that actually break a deploy: a selector that does not match its
template labels, a container without a readiness probe, a Service pointing at nothing.

    python -m tools.tasks.k8s_validate
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

from tools.common.config import ROOT

K8S_DIR = ROOT / "infra" / "k8s"
NAMESPACE = "sentinel"

# Deployments/StatefulSets that must exist and be healthy before the stack is usable.
REQUIRED = {"postgres", "clickhouse", "go-fetcher", "metabase"}


def load(path: Path) -> list[dict]:
    documents = []
    for document in yaml.safe_load_all(path.read_text(encoding="utf-8")):
        if document:
            documents.append(document)
    return documents


def main() -> int:
    problems: list[str] = []
    found_workloads: set[str] = set()
    found_services: set[str] = set()

    for path in sorted(K8S_DIR.glob("*.yaml")):
        if path.name == "kind-config.yaml":
            continue  # kind's own config, not a Kubernetes object
        try:
            documents = load(path)
        except yaml.YAMLError as exc:
            problems.append(f"{path.name}: invalid YAML: {exc}")
            continue

        for document in documents:
            kind = document.get("kind")
            name = (document.get("metadata") or {}).get("name")
            namespace = (document.get("metadata") or {}).get("namespace")

            if kind not in {"Namespace"} and namespace != NAMESPACE:
                problems.append(f"{path.name}: {kind}/{name} is not in namespace {NAMESPACE}")

            if kind in {"Deployment", "StatefulSet"}:
                found_workloads.add(name)
                spec = document.get("spec") or {}
                selector = (spec.get("selector") or {}).get("matchLabels") or {}
                template_labels = (
                    ((spec.get("template") or {}).get("metadata") or {}).get("labels") or {}
                )
                if not selector:
                    problems.append(f"{path.name}: {kind}/{name} has no selector")
                elif not all(template_labels.get(k) == v for k, v in selector.items()):
                    # A selector that does not match its own pod template produces a
                    # Deployment that is created and never schedules anything.
                    problems.append(f"{path.name}: {kind}/{name} selector does not match pod labels")

                containers = ((spec.get("template") or {}).get("spec") or {}).get("containers") or []
                if not containers:
                    problems.append(f"{path.name}: {kind}/{name} has no containers")
                for container in containers:
                    if not container.get("readinessProbe"):
                        problems.append(
                            f"{path.name}: {kind}/{name}/{container.get('name')} has no readinessProbe"
                        )
                    if not (container.get("resources") or {}).get("limits"):
                        problems.append(
                            f"{path.name}: {kind}/{name}/{container.get('name')} has no resource limits"
                        )

            if kind == "Service":
                found_services.add(name)
                if not (document.get("spec") or {}).get("selector"):
                    problems.append(f"{path.name}: Service/{name} has no selector")

    missing_workloads = REQUIRED - found_workloads
    if missing_workloads:
        problems.append(f"missing workloads: {', '.join(sorted(missing_workloads))}")

    # Every workload should have a Service, or nothing can reach it.
    for workload in found_workloads:
        if workload not in found_services:
            problems.append(f"workload {workload} has no Service")

    print(f"manifests: {len(list(K8S_DIR.glob('*.yaml'))) - 1}")
    print(f"workloads: {', '.join(sorted(found_workloads))}")
    print(f"services:  {', '.join(sorted(found_services))}")
    print()

    if problems:
        for problem in problems:
            print(f"  FAIL {problem}")
        return 1
    print("PASS: manifests are structurally sound")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

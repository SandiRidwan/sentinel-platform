"""Deploy the SENTINEL stack to a local kind cluster.

Plain manifests rather than Helm: three Deployments, a StatefulSet and three Services do not
need a templating layer, and a reader can see the whole object without rendering it first.

    python -m tools.tasks.k8s up
    python -m tools.tasks.k8s status
    python -m tools.tasks.k8s down
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

from tools.common.config import ROOT

K8S_DIR = ROOT / "infra" / "k8s"
CLUSTER = "sentinel"
NAMESPACE = "sentinel"
GO_FETCHER_IMAGE = "sentinel/go-fetcher:local"

# Applied in this order: the namespace and config must exist before anything that
# references them, and postgres must be up before metabase can migrate.
MANIFESTS = [
    "namespace.yaml",
    "postgres.yaml",
    "clickhouse.yaml",
    "go-fetcher.yaml",
    "metabase.yaml",
]

DEPLOYMENTS = ["postgres", "clickhouse", "go-fetcher", "metabase"]


def run(cmd: list[str], *, check: bool = True, capture: bool = False) -> subprocess.CompletedProcess:
    print(f">> {' '.join(cmd)}")
    return subprocess.run(cmd, check=check, capture_output=capture, text=True)


def have(tool: str) -> bool:
    import shutil

    return shutil.which(tool) is not None


def cluster_exists() -> bool:
    result = run(["kind", "get", "clusters"], capture=True, check=False)
    return CLUSTER in (result.stdout or "").split()


def up() -> int:
    if not have("kind"):
        print("  FAIL: kind is not installed. Install it, or use 'make up' (docker compose).")
        return 1
    if not have("kubectl"):
        print("  FAIL: kubectl is not installed.")
        return 1

    if not cluster_exists():
        run(["kind", "create", "cluster", "--config", str(K8S_DIR / "kind-config.yaml")])
    else:
        print(f"  cluster '{CLUSTER}' already exists")

    # Build the fetcher image and load it into the cluster: the node then has it locally and
    # never needs registry credentials for a build that only exists on this machine.
    run([
        "docker", "build",
        "-t", GO_FETCHER_IMAGE,
        str(ROOT / "ingestion" / "go-fetcher"),
    ])
    run(["kind", "load", "docker-image", GO_FETCHER_IMAGE, "--name", CLUSTER])

    for manifest in MANIFESTS:
        run(["kubectl", "apply", "-f", str(K8S_DIR / manifest)])

    print()
    print("waiting for rollout")
    for deployment in DEPLOYMENTS:
        # Metabase needs minutes on first boot (it runs its own migrations).
        timeout = "600s" if deployment == "metabase" else "180s"
        result = run(
            ["kubectl", "-n", NAMESPACE, "rollout", "status", f"deployment/{deployment}", f"--timeout={timeout}"],
            check=False,
        )
        if result.returncode != 0 and deployment != "postgres":
            print(f"  warning: {deployment} did not roll out in time")

    return status()


def status() -> int:
    run(["kubectl", "-n", NAMESPACE, "get", "pods", "-o", "wide"], check=False)
    print()
    print("endpoints (kind maps node ports to the host):")
    print("  go-fetcher  http://localhost:8088/healthz")
    print("  clickhouse  http://localhost:8123/ping")
    print("  metabase    http://localhost:3000")
    return 0


def down() -> int:
    if not have("kind"):
        print("  kind not installed; nothing to remove")
        return 0
    if not cluster_exists():
        print(f"  cluster '{CLUSTER}' does not exist")
        return 0
    run(["kind", "delete", "cluster", "--name", CLUSTER])
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Manage the local kind cluster.")
    parser.add_argument("action", choices=["up", "down", "status"], nargs="?", default="status")
    args = parser.parse_args(argv)

    return {"up": up, "down": down, "status": status}[args.action]()


if __name__ == "__main__":
    raise SystemExit(main())

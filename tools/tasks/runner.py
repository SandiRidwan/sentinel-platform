"""SENTINEL task runner.

One implementation, two entry points (Makefile and tasks.ps1). Keeping the logic
here means `make up` and `.\tasks.ps1 up` can never drift apart.

Every command returns a process exit code: 0 pass, non-zero fail. Nothing here
swallows an error to look successful.
"""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
IS_WINDOWS = platform.system() == "Windows"

# Always call back into the interpreter that is running this script, so every
# subprocess stays inside the project virtualenv instead of falling back to the
# system python.
PY = sys.executable

# ---------------------------------------------------------------- output ----

_NO_COLOR = os.environ.get("NO_COLOR") is not None


def _c(code: str, text: str) -> str:
    return text if _NO_COLOR else f"\033[{code}m{text}\033[0m"


def info(msg: str) -> None:
    print(f"{_c('36', '>>')} {msg}")


def ok(msg: str) -> None:
    print(f"{_c('32', 'OK')} {msg}")


def warn(msg: str) -> None:
    print(f"{_c('33', '!!')} {msg}")


def fail(msg: str) -> None:
    print(f"{_c('31', 'XX')} {msg}")


def header(title: str) -> None:
    line = "=" * 68
    print(f"\n{_c('35', line)}\n{_c('35', title)}\n{_c('35', line)}")


# ------------------------------------------------------------- running ----


def run(
    cmd: list[str],
    *,
    cwd: Path | None = None,
    check: bool = True,
    capture: bool = False,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    """Run a command, echoing it first. Raises on failure when check=True."""
    where = cwd or ROOT
    printable = " ".join(cmd)
    info(f"$ {printable}")
    full_env = {**os.environ, **(env or {})}
    completed = subprocess.run(
        cmd,
        cwd=str(where),
        env=full_env,
        capture_output=capture,
        text=True,
    )
    if check and completed.returncode != 0:
        if capture:
            if completed.stdout:
                print(completed.stdout)
            if completed.stderr:
                print(completed.stderr, file=sys.stderr)
        fail(f"command failed with exit code {completed.returncode}: {printable}")
        raise SystemExit(completed.returncode)
    return completed


def have(tool: str) -> bool:
    return shutil.which(tool) is not None


def tool_version(cmd: list[str]) -> str | None:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if out.returncode == 0:
            return (out.stdout or out.stderr).strip().splitlines()[0]
    except (OSError, subprocess.SubprocessError):
        return None
    return None


# ----------------------------------------------------------- commands ----


def cmd_doctor(_args: argparse.Namespace) -> int:
    """Verify every tool and service the platform needs."""
    header("SENTINEL — environment doctor")

    tools = [
        ("python", [PY, "--version"], True),
        ("docker", ["docker", "--version"], True),
        ("docker compose", ["docker", "compose", "version"], True),
        ("go", ["go", "version"], True),
        ("terraform", ["terraform", "version"], True),
        ("kubectl", ["kubectl", "version", "--client"], True),
        ("make", ["make", "--version"], False),
    ]

    failures = 0
    for name, probe, required in tools:
        version = tool_version(probe)
        if version:
            ok(f"{name:16s} {version}")
        elif required:
            fail(f"{name:16s} MISSING (required)")
            failures += 1
        else:
            warn(f"{name:16s} missing (optional on Windows; tasks.ps1 is equivalent)")

    print()
    info("python packages")
    for module, label in [
        ("scrapy", "scrapy"), ("dbt", "dbt"), ("pyspark", "pyspark"),
        ("great_expectations", "great_expectations"), ("pandas", "pandas"),
        ("pyarrow", "pyarrow"), ("duckdb", "duckdb"),
    ]:
        probe = tool_version([PY, "-c", f"import {module}; print('{label} ok')"])
        if probe:
            ok(f"{label:20s} {probe}")
        else:
            warn(f"{label:20s} not installed (make deps)")

    print()
    info("services (only meaningful after `up`)")
    services = [
        ("s3 (s3mock)", f"{_settings().s3.endpoint_url}/"),
        ("postgres", None),
        ("clickhouse", f"{_settings().ch.http_url}/ping"),
        ("metabase", f"{os.environ.get('METABASE_URL', 'http://localhost:3000')}/api/health"),
    ]
    for label, url in services:
        if _probe_service(label, url):
            ok(f"{label:16s} reachable")
        else:
            warn(f"{label:16s} not reachable (run: make up)")

    print()
    if failures:
        fail(f"doctor found {failures} required tool(s) missing")
        return 1
    ok("all required tools present")
    return 0


def _settings():
    from tools.common.config import get_settings

    return get_settings()


def _probe_service(label: str, url: str | None) -> bool:
    """HTTP probe where the service has one, a TCP connect otherwise."""
    import socket
    import urllib.request

    if url is None:
        try:
            with socket.create_connection((_settings().pg.host, _settings().pg.port), timeout=3):
                return True
        except OSError:
            return False

    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            # Metabase answers 200 with a body; /ping answers "Ok."; S3 root answers XML.
            return resp.status < 500
    except urllib.error.HTTPError as exc:
        return exc.code < 500
    except Exception:
        return False


def _compose_files() -> list[str]:
    files = ["-f", "docker-compose.yml"]
    override = ROOT / "docker-compose.override.yml"
    if override.exists():
        files += ["-f", "docker-compose.override.yml"]
    return files


def cmd_up(_args: argparse.Namespace) -> int:
    """Start the local stack (S3, Postgres, ClickHouse, Metabase, Airflow, go-fetcher)."""
    header("starting local stack")
    run(["docker", "compose", *_compose_files(), "up", "-d", "--wait"])
    ok("stack is up")
    return 0


def cmd_down(_args: argparse.Namespace) -> int:
    """Stop the local stack, keeping volumes."""
    header("stopping local stack")
    run(["docker", "compose", *_compose_files(), "down"])
    ok("stack is down (volumes kept)")
    return 0


def cmd_restart(_args: argparse.Namespace) -> int:
    """Restart the local stack."""
    header("restarting local stack")
    run(["docker", "compose", *_compose_files(), "restart"])
    return 0


def cmd_logs(_args: argparse.Namespace) -> int:
    """Tail stack logs."""
    run(["docker", "compose", *_compose_files(), "logs", "--tail=100", "-f"])
    return 0


def cmd_seed(_args: argparse.Namespace) -> int:
    """Create buckets and database objects."""
    header("seeding buckets and databases")
    run([PY, "-m", "tools.seed"])
    ok("seed complete")
    return 0


def cmd_scrape(_args: argparse.Namespace) -> int:
    """Run the spiders against declared sources."""
    header("running spiders")
    run([PY, "-m", "ingestion.scrapy.sentinel_spiders.run_ingest"])
    return 0


def cmd_quality(_args: argparse.Namespace) -> int:
    """Run all four quality levels; fail if any level fails."""
    header("quality — all four levels")
    rc = 0
    for label, cmd in [
        ("L1 unit (pytest)", [PY, "-m", "pytest", "quality/unit", "-q"]),
        ("L2 contract (scrapy check)", [PY, "-m", "quality.contract.run_contracts"]),
        ("L3 warehouse (dbt test)", [PY, "-m", "quality.warehouse.run_warehouse_tests"]),
        ("L4 expectations", [PY, "-m", "quality.expectations.run_suites"]),
    ]:
        print()
        info(label)
        result = run(cmd, check=False)
        if result.returncode != 0:
            fail(f"{label} FAILED")
            rc = result.returncode or 1
        else:
            ok(f"{label} passed")
    if rc == 0:
        ok("all quality levels passed")
    return rc


def cmd_test(_args: argparse.Namespace) -> int:
    """Run unit tests only (fast)."""
    return run([PY, "-m", "pytest", "quality/unit", "-q"], check=False).returncode


def cmd_docs(_args: argparse.Namespace) -> int:
    """Regenerate dbt docs."""
    header("generating dbt docs")
    run([PY, "-m", "tools.tasks.dbt", "docs", "generate"])
    return 0


def cmd_clickhouse_load(_args: argparse.Namespace) -> int:
    """Load the gold layer into ClickHouse."""
    header("loading clickhouse")
    run([PY, "-m", "analytics.clickhouse.load"])
    return 0


def cmd_metabase_import(_args: argparse.Namespace) -> int:
    """Create the Metabase dashboards from their JSON definitions."""
    header("importing metabase dashboards")
    run([PY, "-m", "analytics.metabase.import_dashboards"])
    return 0



def cmd_pii_scan(_args: argparse.Namespace) -> int:
    """Scan the warehouse for PII and report what it finds."""
    header("pii scan")
    run([PY, "-m", "governance.pii.scan"])
    return 0


def cmd_policy_apply(_args: argparse.Namespace) -> int:
    """Validate and apply the column-security policy."""
    header("column security")
    run([PY, "-m", "governance.policies.apply"])
    return 0


def cmd_lineage(_args: argparse.Namespace) -> int:
    """Emit and render the lineage graph."""
    header("lineage")
    run([PY, "-m", "tools.lineage"])
    return 0


def cmd_infra_plan(_args: argparse.Namespace) -> int:
    """terraform plan for the dev environment."""
    header("terraform plan (dev)")
    run(["terraform", "init", "-input=false"], cwd=ROOT / "infra" / "terraform")
    run(["terraform", "plan", "-input=false", "-var-file=dev.tfvars"], cwd=ROOT / "infra" / "terraform")
    return 0


def cmd_infra_destroy(_args: argparse.Namespace) -> int:
    """terraform destroy for the dev environment."""
    header("terraform destroy (dev)")
    run(["terraform", "destroy", "-auto-approve", "-input=false", "-var-file=dev.tfvars"],
        cwd=ROOT / "infra" / "terraform")
    return 0


def cmd_k8s_up(_args: argparse.Namespace) -> int:
    """Deploy the stack to a local kind cluster."""
    header("deploying to local kind cluster")
    run([PY, "-m", "tools.tasks.k8s", "up"])
    return 0


def cmd_k8s_down(_args: argparse.Namespace) -> int:
    """Remove the local kind cluster."""
    header("removing local kind cluster")
    run([PY, "-m", "tools.tasks.k8s", "down"])
    return 0


def cmd_clean(_args: argparse.Namespace) -> int:
    """Remove generated data, caches and build output."""
    header("cleaning generated output")
    import shutil as _sh

    targets = [
        "data/lake", "data/warehouse", ".pytest_cache", ".ruff_cache",
        "transform/dbt/target", "transform/dbt/logs", "orchestration/logs",
    ]
    for rel in targets:
        path = ROOT / rel
        if path.exists():
            _sh.rmtree(path, ignore_errors=True) if path.is_dir() else path.unlink()
            info(f"removed {rel}")
    ok("clean complete")
    return 0


def cmd_all(_args: argparse.Namespace) -> int:
    """doctor -> up -> seed -> scrape -> quality."""
    for fn in (cmd_doctor, cmd_up, cmd_seed, cmd_scrape, cmd_quality):
        rc = fn(argparse.Namespace())
        if rc != 0:
            fail(f"pipeline stopped: {fn.__name__} returned {rc}")
            return rc
    return 0


COMMANDS = {
    "doctor": cmd_doctor,
    "up": cmd_up,
    "down": cmd_down,
    "restart": cmd_restart,
    "logs": cmd_logs,
    "seed": cmd_seed,
    "scrape": cmd_scrape,
    "quality": cmd_quality,
    "test": cmd_test,
    "docs": cmd_docs,
    "clickhouse-load": cmd_clickhouse_load,
    "metabase-import": cmd_metabase_import,
    "pii-scan": cmd_pii_scan,
    "policy-apply": cmd_policy_apply,
    "lineage": cmd_lineage,
    "infra-plan": cmd_infra_plan,
    "infra-destroy": cmd_infra_destroy,
    "k8s-up": cmd_k8s_up,
    "k8s-down": cmd_k8s_down,
    "clean": cmd_clean,
    "all": cmd_all,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="sentinel",
        description="SENTINEL task runner. Same commands from make or tasks.ps1.",
    )
    parser.add_argument("command", nargs="?", default="help", choices=[*COMMANDS, "help"])
    parser.add_argument("rest", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)

    if args.command == "help":
        print("\nSENTINEL tasks:\n")
        for name, fn in COMMANDS.items():
            # Some commands have no docstring; help must still list them rather than crash.
            doc_lines = (fn.__doc__ or "").strip().splitlines()
            summary = doc_lines[0].strip() if doc_lines else ""
            print(f"  {name:18s} {summary}")
        print()
        return 0

    return COMMANDS[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())

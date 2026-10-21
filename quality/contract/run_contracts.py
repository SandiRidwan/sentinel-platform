"""Quality level 2 — spider contracts.

`scrapy check` runs the `@url` / `@returns` / `@scrapes` declarations in each spider's
docstrings against the live page. This is the level that catches the failure everyone
eventually ships: the site changed its markup, the spider returned far fewer rows, and
nothing raised.

    python -m quality.contract.run_contracts
    python -m quality.contract.run_contracts --spider books
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from tools.common.logging_setup import get_logger, setup_logging

log = get_logger("quality.contracts")

DEFAULT_SPIDERS = ("books", "book_categories", "quotes", "countries", "hockey")


def run_contracts(spiders: tuple[str, ...], timeout: int = 900) -> dict:
    env = {
        **os.environ,
        "SCRAPY_SETTINGS_MODULE": "ingestion.scrapy.sentinel_spiders.settings",
        "SENTINEL_LOG_LEVEL": "WARNING",
    }

    per_spider: dict[str, dict] = {}
    for spider in spiders:
        cmd = [sys.executable, "-m", "scrapy", "check", spider]
        log.info("checking contracts", extra={"event": "contract_start", "spider": spider})
        completed = subprocess.run(
            cmd,
            cwd=str(Path.cwd()),
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        output = (completed.stdout or "") + (completed.stderr or "")
        failures = [
            line.strip()
            for line in output.splitlines()
            if "FAILED" in line.upper() or "Error" in line and "contract" in line.lower()
        ]
        per_spider[spider] = {
            "returncode": completed.returncode,
            "failures": failures,
            "tail": "\n".join(output.splitlines()[-15:]),
        }
        state = "PASS" if completed.returncode == 0 else "FAIL"
        print(f"  {spider:<18} {state}")
        if completed.returncode != 0:
            for line in failures[:5]:
                print(f"      {line}")

    failed = [name for name, result in per_spider.items() if result["returncode"] != 0]
    return {
        "level": "L2_contract",
        "spiders": len(spiders),
        "failed": failed,
        "passed": len(spiders) - len(failed),
        "detail": per_spider,
    }


def main(argv: list[str] | None = None) -> int:
    setup_logging()
    parser = argparse.ArgumentParser(description="Run Scrapy contract tests.")
    parser.add_argument("--spider", action="append")
    args = parser.parse_args(argv)

    spiders = tuple(args.spider) if args.spider else DEFAULT_SPIDERS
    started = datetime.now(timezone.utc)
    print(f"Contract tests ({len(spiders)} spiders)")

    report = run_contracts(spiders)
    report["ran_at"] = started.isoformat()
    report["elapsed_s"] = round((datetime.now(timezone.utc) - started).total_seconds(), 2)

    path = Path("quality/reports/contracts.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print()
    if report["failed"]:
        print(f"FAILED: {', '.join(report['failed'])}")
        return 1
    print(f"PASS: {report['passed']}/{report['spiders']} spiders")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

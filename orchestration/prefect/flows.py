"""Prefect wrapper. The work is in orchestration.shared.pipeline; this only schedules it.

Kept thin on purpose: if this file ever contains a transformation, the three-way parity
check stops proving that the orchestrators agree and starts proving that three copies of the
logic agree, which is a much weaker claim.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from prefect import flow, task

from orchestration.shared.pipeline import run_pipeline


@task(name="transform-and-build", retries=1, retry_delay_seconds=10)
def transform_and_build() -> dict:
    return run_pipeline()


@flow(name="sentinel-pipeline-prefect")
def sentinel_pipeline() -> dict:
    return transform_and_build()


def main() -> int:
    result = sentinel_pipeline()
    payload = {"orchestrator": "prefect", **result}

    out = os.environ.get("SENTINEL_PARITY_OUT")
    if out:
        Path(out).write_text(json.dumps(payload), encoding="utf-8")

    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Dagster wrapper. Same shared function, expressed as an asset.

An asset rather than an op, because the point of the comparison is that the three
orchestrators agree on the OUTPUT, and an asset names that output. The asset does not
declare its own computation; it delegates to the shared module.

`main()` goes through `materialize`, not through a direct function call. Calling the
function would run the Python and skip Dagster entirely, which would make the parity check
compare two orchestrators and one plain function call.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from dagster import Definitions, asset, materialize

from orchestration.shared.pipeline import run_pipeline


@asset(name="gold_row_counts", group_name="sentinel")
def gold_row_counts() -> dict:
    """Rows per gold table after transform and dbt build."""
    return run_pipeline()


defs = Definitions(assets=[gold_row_counts])


def main() -> int:
    result = materialize([gold_row_counts])
    payload = {"orchestrator": "dagster", **result.output_for_node("gold_row_counts")}

    out = os.environ.get("SENTINEL_PARITY_OUT")
    if out:
        Path(out).write_text(json.dumps(payload), encoding="utf-8")

    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

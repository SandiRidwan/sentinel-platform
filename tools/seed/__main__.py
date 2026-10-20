"""Seed the local stack: buckets, database objects, reference data.

Idempotent. Running it twice changes nothing and reports the same result, which is what
makes it safe to call from `make seed` on every clone.
"""

from __future__ import annotations

from tools.common.logging_setup import get_logger, setup_logging
from tools.common.s3 import ensure_buckets, list_buckets

log = get_logger("seed")


def main() -> int:
    setup_logging()
    log.info("seeding object storage")
    created = ensure_buckets()

    present = list_buckets()
    log.info("buckets present", extra={"event": "buckets", "rows": len(present)})
    for bucket in present:
        print(f"  bucket  {bucket}")

    if created:
        print(f"  created {len(created)}: {', '.join(created)}")
    else:
        print("  created 0 (all present)")

    print()
    print("Seed complete. Database objects are created by tools/seed/sql on first Postgres init.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

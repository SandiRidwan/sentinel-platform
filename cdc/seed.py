"""Create the operational source table and drive a deterministic change set.

Two modes:

    python -m cdc.seed            # reset to the baseline (120 products)
    python -m cdc.seed --mutate   # reset, then apply a fixed set of changes

`--mutate` exists so the CDC demo is reproducible: the same 120 product ids, the
same 10 updates, 3 deletes and 5 inserts on every run. A demo that depends on
whatever happened to be in the table last time is not evidence of anything.
"""

from __future__ import annotations

import argparse
from decimal import Decimal
from pathlib import Path

import psycopg2
from psycopg2.extras import execute_values

from tools.common.config import get_settings
from tools.common.logging_setup import get_logger, setup_logging

log = get_logger("cdc.seed")

SCHEMA_FILE = Path(__file__).with_name("schema.sql")

BASELINE_ROWS = 120
CATEGORIES = ("books", "electronics", "garden", "kitchen", "office", "toys")

# The mutation set is a constant, not random: the parity check compares final states,
# and a random set would make a failure impossible to reproduce.
UPDATE_IDS = ("P0003", "P0011", "P0024", "P0038", "P0047", "P0056", "P0069", "P0081", "P0093", "P0104")
DELETE_IDS = ("P0007", "P0052", "P0118")
INSERT_IDS = ("P9001", "P9002", "P9003", "P9004", "P9005")


def baseline_row(index: int) -> tuple:
    product_id = f"P{index:04d}"
    return (
        product_id,
        f"Product {index:04d}",
        CATEGORIES[index % len(CATEGORIES)],
        Decimal("10.00") + Decimal(index % 97) * Decimal("1.25"),
        "USD",
        index % 5 != 0,
    )


def connect():
    settings = get_settings()
    return psycopg2.connect(settings.pg.psycopg_dsn)


def apply_schema(conn) -> None:
    sql = SCHEMA_FILE.read_text(encoding="utf-8")
    with conn, conn.cursor() as cur:
        # psycopg2 sends the whole script as one PQexec, so the DO $$ ... $$ block and
        # the semicolons inside it are handled by the server, not split by us.
        cur.execute(sql)
    log.info("schema applied", extra={"file": SCHEMA_FILE.name})


def reset(conn) -> int:
    """Delete every row, then insert the baseline.

    DELETE rather than TRUNCATE: TRUNCATE produces a single truncate event that the
    consumer would have to special-case, while row deletes are the same shape as every
    other change. The demo is worth more than the few milliseconds TRUNCATE saves.
    """
    with conn, conn.cursor() as cur:
        cur.execute("DELETE FROM ops.product_price")
        rows = [baseline_row(i) for i in range(1, BASELINE_ROWS + 1)]
        execute_values(
            cur,
            "INSERT INTO ops.product_price "
            "(product_id, product_name, category, price, currency, in_stock) VALUES %s",
            rows,
            page_size=100,
        )
    log.info("baseline written", extra={"rows": len(rows)})
    return len(rows)


def mutate(conn) -> dict[str, int]:
    """Apply the fixed change set: 10 updates, 3 deletes, 5 inserts."""
    with conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE ops.product_price
               SET price = price + 1.00,
                   in_stock = NOT in_stock,
                   updated_at = now()
             WHERE product_id = ANY(%s)
            """,
            (list(UPDATE_IDS),),
        )
        updated = cur.rowcount

        cur.execute(
            "DELETE FROM ops.product_price WHERE product_id = ANY(%s)",
            (list(DELETE_IDS),),
        )
        deleted = cur.rowcount

        rows = [
            (
                pid,
                f"Product {pid} (new)",
                CATEGORIES[i % len(CATEGORIES)],
                Decimal("99.00") + Decimal(i),
                "USD",
                True,
            )
            for i, pid in enumerate(INSERT_IDS)
        ]
        execute_values(
            cur,
            "INSERT INTO ops.product_price "
            "(product_id, product_name, category, price, currency, in_stock) VALUES %s",
            rows,
            page_size=100,
        )
        inserted = len(rows)

    log.info("mutations applied", extra={"updated": updated, "deleted": deleted, "inserted": inserted})
    return {"updated": updated, "deleted": deleted, "inserted": inserted}


def main(argv: list[str] | None = None) -> int:
    setup_logging()
    parser = argparse.ArgumentParser(description="Seed the CDC source table.")
    parser.add_argument("--mutate", action="store_true", help="also apply the fixed change set")
    args = parser.parse_args(argv)

    conn = connect()
    try:
        apply_schema(conn)
        written = reset(conn)
        result = {"baseline_rows": written}
        if args.mutate:
            result["mutations"] = mutate(conn)
        print(result)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

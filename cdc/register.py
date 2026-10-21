"""Register the Debezium Postgres connector and wait until it is actually running.

`POST /connectors` returning 201 only means the config was accepted. The connector can
still fail a second later — wrong credentials, `wal_level` still `replica`, the
publication missing — and the failure shows up in the status endpoint, not the response.
So this waits for `connector.state == RUNNING` and `tasks[0].state == RUNNING` before it
reports success. Anything else is printed with the trace and exits non-zero.

    python -m cdc.register
    python -m cdc.register --delete
"""

from __future__ import annotations

import argparse
import json
import time

import requests

from tools.common.config import get_settings
from tools.common.logging_setup import get_logger, setup_logging

log = get_logger("cdc.register")

CONNECTOR_NAME = "sentinel-product-price"
TOPIC = "sentinel.ops.product_price"
CONNECT_URL = get_settings().kafka.connect_url

# `decimal.handling.mode=string` is the one setting that changes the answer rather than
# the format: the default (precise) ships NUMERIC as base64-encoded bytes, and the
# `double` mode rounds. A price that rounds is a price that disagrees with the source,
# and the parity check would then fail for a reason that has nothing to do with CDC.
CONNECTOR_CONFIG = {
    "connector.class": "io.debezium.connector.postgresql.PostgresConnector",
    "tasks.max": "1",
    "database.hostname": "postgres",
    "database.port": "5432",
    "database.dbname": "sentinel",
    "topic.prefix": "sentinel",
    "table.include.list": "ops.product_price",
    "plugin.name": "pgoutput",
    "publication.name": "sentinel_cdc",
    "slot.name": "sentinel_cdc_slot",
    "decimal.handling.mode": "string",
    "time.precision.mode": "adaptive_time_microseconds",
    "tombstones.on.delete": "true",
    "heartbeat.interval.ms": "10000",
    "snapshot.mode": "initial",
    # Only the operations the consumer implements. `t` (truncate) is excluded because
    # cdc/seed.py never truncates; if it did, the consumer would silently ignore the
    # event and the two paths would disagree with no error anywhere.
    "skipped.operations": "t",
}


def config_with_credentials() -> dict:
    settings = get_settings()
    config = dict(CONNECTOR_CONFIG)
    config["database.user"] = settings.pg.user
    config["database.password"] = settings.pg.password
    return config


def wait_for_connect(timeout_s: int = 120) -> None:
    deadline = time.time() + timeout_s
    last_error = ""
    while time.time() < deadline:
        try:
            response = requests.get(f"{CONNECT_URL}/connectors", timeout=5)
            if response.status_code == 200:
                return
            last_error = f"HTTP {response.status_code}"
        except requests.RequestException as exc:
            last_error = str(exc)
        time.sleep(2)
    raise SystemExit(f"Kafka Connect is not answering at {CONNECT_URL}: {last_error}")


def delete_connector() -> None:
    response = requests.delete(f"{CONNECT_URL}/connectors/{CONNECTOR_NAME}", timeout=10)
    if response.status_code in (204, 404):
        print(f"connector {CONNECTOR_NAME}: absent")
    else:
        print(f"connector {CONNECTOR_NAME}: delete returned HTTP {response.status_code}")


def register() -> dict:
    delete_connector()
    payload = {"name": CONNECTOR_NAME, "config": config_with_credentials()}
    response = requests.post(f"{CONNECT_URL}/connectors", json=payload, timeout=30)
    if response.status_code not in (200, 201):
        raise SystemExit(f"connector create failed: HTTP {response.status_code} {response.text}")
    return response.json()


def wait_for_running(timeout_s: int = 120) -> dict:
    deadline = time.time() + timeout_s
    status: dict = {}
    while time.time() < deadline:
        response = requests.get(f"{CONNECT_URL}/connectors/{CONNECTOR_NAME}/status", timeout=10)
        status = response.json()
        connector_state = status.get("connector", {}).get("state")
        tasks = status.get("tasks", [])
        task_states = [task.get("state") for task in tasks]
        if connector_state == "RUNNING" and task_states and all(s == "RUNNING" for s in task_states):
            return status
        if connector_state == "FAILED" or any(s == "FAILED" for s in task_states):
            trace = status.get("connector", {}).get("trace") or next(
                (t.get("trace") for t in tasks if t.get("trace")), ""
            )
            raise SystemExit(f"connector FAILED:\n{trace}")
        time.sleep(2)
    raise SystemExit(f"connector did not reach RUNNING in {timeout_s}s: {json.dumps(status, indent=2)}")


def main(argv: list[str] | None = None) -> int:
    setup_logging()
    parser = argparse.ArgumentParser(description="Register the Debezium Postgres connector.")
    parser.add_argument("--delete", action="store_true", help="remove the connector and exit")
    args = parser.parse_args(argv)

    wait_for_connect()
    if args.delete:
        delete_connector()
        return 0

    register()
    status = wait_for_running()
    print(
        json.dumps(
            {
                "connector": CONNECTOR_NAME,
                "state": status["connector"]["state"],
                "task_states": [t["state"] for t in status.get("tasks", [])],
                "topic": TOPIC,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

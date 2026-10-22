"""Create the SENTINEL Metabase dashboards from the JSON definitions.

Why this exists: a dashboard built by clicking is a dashboard nobody can rebuild. It lives
in one person's browser, it is not reviewed, and when the instance is replaced it is gone.
Defining it in the repository makes it reviewable and reproducible.

Idempotent: an existing dashboard with the same name is skipped rather than duplicated.

    python -m analytics.metabase.import_dashboards
    python -m analytics.metabase.import_dashboards --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

from tools.common.config import get_settings
from tools.common.logging_setup import get_logger, setup_logging

log = get_logger("metabase")

# Metabase rejects a password on its common-password list and an email on a reserved TLD,
# so these are real values from .env rather than convenient placeholders.
ADMIN_EMAIL = os.environ.get("METABASE_ADMIN_EMAIL", "admin@sentinel.example")
ADMIN_PASSWORD = os.environ.get("METABASE_ADMIN_PASSWORD", "S3ntinel-L0cal-2026")

DEFINITIONS = Path(__file__).parent / "dashboards" / "sentinel_dashboards.json"


def wait_ready(base: str, timeout_s: int = 180) -> bool:
    """Metabase takes a minute or two to run migrations on first boot."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            response = httpx.get(f"{base}/api/health", timeout=5)
            if response.status_code == 200 and response.json().get("status") == "ok":
                return True
        except httpx.HTTPError:
            pass
        time.sleep(3)
    return False


def ensure_setup(client: httpx.Client, base: str) -> None:
    """Complete first-run setup if it has not happened yet."""
    properties = client.get(f"{base}/api/session/properties").json()
    token = properties.get("setup-token")
    if not token:
        return  # already set up

    payload = {
        "token": token,
        "user": {
            "first_name": "Sentinel",
            "last_name": "Admin",
            "email": ADMIN_EMAIL,
            "password": ADMIN_PASSWORD,
            "site_name": "SENTINEL",
        },
        "prefs": {"site_name": "SENTINEL", "allow_tracking": False},
    }
    response = client.post(f"{base}/api/setup", json=payload)
    if response.status_code not in (200, 201, 202):
        log.warning("setup returned %s: %s", response.status_code, response.text[:200])


def authenticate(client: httpx.Client, base: str) -> str:
    response = client.post(
        f"{base}/api/session",
        json={"username": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
    )
    if response.status_code != 200:
        raise RuntimeError(f"login failed ({response.status_code}): {response.text[:200]}")
    return response.json()["id"]


def ensure_clickhouse_database(client: httpx.Client, base: str) -> int:
    settings = get_settings()
    existing = client.get(f"{base}/api/database").json()
    databases = existing.get("data", existing) if isinstance(existing, dict) else existing
    for database in databases:
        if database.get("name") == "sentinel-clickhouse":
            return database["id"]

    payload = {
        "name": "sentinel-clickhouse",
        "engine": "clickhouse",
        "details": {
            # Metabase runs inside the compose network, so "localhost" would mean its own
            # container. The compose service name is the address it can actually reach.
            "host": os.environ.get("METABASE_CLICKHOUSE_HOST", "clickhouse"),
            "port": settings.ch.port,
            "user": settings.ch.user,
            "password": settings.ch.password,
            "db": settings.ch.db,
            "ssl": False,
        },
    }
    response = client.post(f"{base}/api/database", json=payload)
    if response.status_code not in (200, 201):
        raise RuntimeError(f"database create failed ({response.status_code}): {response.text[:300]}")
    return response.json()["id"]


def create_card(client: httpx.Client, base: str, database_id: int, card: dict) -> int:
    payload = {
        "name": card["name"],
        "display": card.get("display", "table"),
        "dataset_query": {
            "type": "native",
            "native": {"query": card["query"], "template-tags": {}},
            "database": database_id,
        },
        "visualization_settings": card.get("visualization_settings", {}),
    }
    response = client.post(f"{base}/api/card", json=payload)
    if response.status_code not in (200, 201, 202):
        raise RuntimeError(f"card '{card['name']}' failed ({response.status_code}): {response.text[:300]}")
    return response.json()["id"]


def create_dashboard(client: httpx.Client, base: str, database_id: int, definition: dict) -> int:
    dashboard = client.post(
        f"{base}/api/dashboard",
        json={"name": definition["name"], "description": definition.get("description", "")},
    )
    if dashboard.status_code not in (200, 201):
        raise RuntimeError(f"dashboard failed ({dashboard.status_code}): {dashboard.text[:300]}")
    dashboard_id = dashboard.json()["id"]

    cards = []
    for index, card in enumerate(definition.get("cards", [])):
        card_id = create_card(client, base, database_id, card)
        cards.append({
            "id": -index - 1,
            "card_id": card_id,
            "row": 0,
            "col": (index % 2) * 9,
            "size_x": 9,
            "size_y": 6,
            "parameter_mappings": [],
            "visualization_settings": {},
        })

    if cards:
        update = client.put(
            f"{base}/api/dashboard/{dashboard_id}",
            json={"dashcards": cards},
        )
        if update.status_code not in (200, 202):
            # Layout API differs between versions; the cards exist either way, so this is
            # reported rather than treated as a failure to create the dashboard.
            log.warning("dashboard layout not applied: %s", update.text[:200])

    return dashboard_id


def main(argv: list[str] | None = None) -> int:
    setup_logging()
    parser = argparse.ArgumentParser(description="Create Metabase dashboards from JSON.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--definitions", default=str(DEFINITIONS))
    args = parser.parse_args(argv)

    definitions = json.loads(Path(args.definitions).read_text(encoding="utf-8"))
    base = os.environ.get("METABASE_URL", "http://localhost:3000").rstrip("/")

    started = datetime.now(timezone.utc)
    print(f"Metabase import ({len(definitions)} dashboards)")

    if args.dry_run:
        for definition in definitions:
            print(f"  would create: {definition['name']} ({len(definition.get('cards', []))} cards)")
        return 0

    if not wait_ready(base):
        print("  FAIL: Metabase did not become ready in time (is the stack up?)")
        return 1

    with httpx.Client(timeout=60) as client:
        ensure_setup(client, base)
        session_id = authenticate(client, base)
        client.headers["X-Metabase-Session"] = session_id

        database_id = ensure_clickhouse_database(client, base)
        print(f"  database: sentinel-clickhouse (id={database_id})")

        existing = client.get(f"{base}/api/dashboard").json()
        names = {d["name"] for d in existing}

        created = []
        for definition in definitions:
            if definition["name"] in names:
                print(f"  {definition['name']:<28} exists, skipped")
                continue
            dashboard_id = create_dashboard(client, base, database_id, definition)
            created.append({"name": definition["name"], "id": dashboard_id})
            print(f"  {definition['name']:<28} created (id={dashboard_id})")

    elapsed = (datetime.now(timezone.utc) - started).total_seconds()
    out = {
        "ran_at": started.isoformat(),
        "elapsed_s": round(elapsed, 2),
        "database_id": database_id,
        "created": created,
        "definitions": len(definitions),
    }
    print()
    print(json.dumps(out, indent=2))

    path = Path("quality/reports/metabase_import.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

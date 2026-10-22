"""Prove the four dashboards are the same dashboard.

Two claims, checked separately because they fail for different reasons:

  1. **Same SQL.** Every rendered definition carries the card's SQL verbatim. This is checked
     by parsing the SQL back out of each rendered file — the Metabase JSON, the Superset
     bundle, the Looker Apps Script, the Sigma workbook — and comparing with the spec. A
     renderer that "adapts" a query fails here.
  2. **Same number.** The SQL runs against ClickHouse, and the result is compared with what
     Metabase returns for the same card through its own API. That is a real end-to-end check:
     two independent paths through a BI tool and a database driver.

Superset, Looker Studio and Sigma are *compiled, not executed*, and the report says so per
target rather than hiding it behind one aggregate "PASS". Superset's stock image does not
ship the ClickHouse driver, Looker Studio needs a Google account, and Sigma's import format
is not published. A report that claimed otherwise would be the exact kind of claim this
repository exists to avoid.

    python -m analytics.parity
    python -m analytics.parity --skip-metabase
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import httpx
import yaml

from analytics.clickhouse.load import clickhouse_client
from analytics.render import load_spec, render_all

REPORT = Path("quality/reports/bi_parity.json")
METABASE = os.environ.get("METABASE_URL", "http://localhost:3000").rstrip("/")
ADMIN_EMAIL = os.environ.get("METABASE_ADMIN_EMAIL", "admin@sentinel.example")
ADMIN_PASSWORD = os.environ.get("METABASE_ADMIN_PASSWORD", "S3ntinel-L0cal-2026")

def normalise(sql: str) -> str:
    return " ".join(sql.split())

# --------------------------------------------------------------------------------------
# Extracting the SQL back out of each rendered artefact.
# --------------------------------------------------------------------------------------

def sql_from_metabase(content: bytes) -> dict[str, str]:
    dashboards = json.loads(content.decode("utf-8"))
    found = {}
    for dashboard in dashboards:
        for card in dashboard["cards"]:
            found[card["name"]] = normalise(card["query"])
    return found

def sql_from_superset(content: bytes) -> dict[str, str]:
    found = {}
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        for name in archive.namelist():
            if not name.startswith("datasets/"):
                continue
            dataset = yaml.safe_load(archive.read(name).decode("utf-8"))
            found[dataset["table_name"]] = normalise(dataset["sql"])
    return found

def sql_from_looker(content: bytes) -> dict[str, str]:
    """The connector embeds a JSON map of cards; read it back out of the generated source."""
    text = content.decode("utf-8")
    match = re.search(r"var CARDS = (\{.*?\n\});\n", text, re.DOTALL)
    if not match:
        raise ValueError("CARDS block not found in the generated connector")
    cards = json.loads(match.group(1))
    return {card_id: normalise(card["sql"]) for card_id, card in cards.items()}

def sql_from_sigma(content: bytes) -> dict[str, str]:
    workbook = json.loads(content.decode("utf-8"))
    found = {}
    for page in workbook["pages"]:
        for element in page["elements"]:
            found[element["id"]] = normalise(element["sql"])
    return found

EXTRACTORS = {
    "metabase": ("metabase/sentinel_dashboards.json", sql_from_metabase, "title"),
    "superset": ("superset/sentinel_dashboard.zip", sql_from_superset, "id"),
    "looker_studio": ("looker_studio/Code.gs", sql_from_looker, "id"),
    "sigma": ("sigma/sentinel_workbook.json", sql_from_sigma, "id"),
}

# --------------------------------------------------------------------------------------
# Execution
# --------------------------------------------------------------------------------------

def clickhouse_rows(sql: str) -> list[list]:
    client = clickhouse_client()
    try:
        result = client.query(sql)
        return [list(row) for row in result.result_rows]
    finally:
        client.close()

def wait_ready(timeout_s: int = 180) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            response = httpx.get(f"{METABASE}/api/health", timeout=5)
            if response.status_code == 200 and response.json().get("status") == "ok":
                return True
        except httpx.HTTPError:
            pass
        time.sleep(3)
    return False

def metabase_cards() -> dict[str, int]:
    """Card ids by name, logging in as the local admin.

    The admin account is created by `analytics.metabase.import_dashboards` on first run; if
    it does not exist the parity check reports that rather than creating users of its own.
    """
    session = httpx.post(
        f"{METABASE}/api/session",
        json={"username": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
        timeout=30,
    )
    if session.status_code != 200:
        raise RuntimeError(f"metabase login failed ({session.status_code})")

    headers = {"X-Metabase-Session": session.json()["id"]}
    cards = httpx.get(f"{METABASE}/api/card", headers=headers, timeout=60).json()
    return {card["name"]: card["id"] for card in cards}

def metabase_rows(card_id: int, headers: dict) -> list[list]:
    response = httpx.post(f"{METABASE}/api/card/{card_id}/query", headers=headers, timeout=120)
    if response.status_code != 202 and response.status_code != 200:
        raise RuntimeError(f"card {card_id} query failed ({response.status_code}): {response.text[:200]}")
    return response.json()["data"]["rows"]

def comparable(rows: list[list]) -> list[list]:
    """Normalise numbers so 15 and 15.0 compare equal across two JSON encoders."""
    normalised = []
    for row in rows:
        normalised.append(
            [
                round(float(value), 6) if isinstance(value, (int, float)) and not isinstance(value, bool) else value
                for value in row
            ]
        )
    return normalised

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compare the same dashboard across BI tools.")
    parser.add_argument("--skip-metabase", action="store_true")
    parser.add_argument("--report", default=str(REPORT))
    args = parser.parse_args(argv)

    started = datetime.now(timezone.utc)
    spec = load_spec()
    rendered = render_all(spec)
    expected = {card["id"]: normalise(card["sql"]) for card in spec["cards"]}

    # 1. Same SQL in every definition.
    sql_checks: dict[str, dict] = {}
    for target, (path, extractor, _key) in EXTRACTORS.items():
        found = extractor(rendered[path])
        # Metabase keys by card title, the rest by card id; map back to ids for comparison.
        if target == "metabase":
            titles = {card["title"]: card["id"] for card in spec["cards"]}
            found = {titles[name]: sql for name, sql in found.items() if name in titles}

        missing = sorted(set(expected) - set(found))
        different = sorted(card_id for card_id, sql in found.items() if expected.get(card_id) != sql)
        sql_checks[target] = {
            "cards": len(found),
            "missing": missing,
            "different": different,
            "identical": not missing and not different,
        }

    # 2. Same number: ClickHouse is the reference, Metabase is the tool under test.
    values: dict[str, dict] = {}
    for card in spec["cards"]:
        try:
            values[card["id"]] = {"clickhouse": comparable(clickhouse_rows(card["sql"]))}
        except Exception as exc:  # noqa: BLE001 - the message is the result
            values[card["id"]] = {"clickhouse": None, "error": f"{type(exc).__name__}: {exc}"}

    executed = ["clickhouse"]
    metabase_status = {"executed": False, "reason": "skipped"}
    headers: dict = {}

    if not args.skip_metabase:
        if not wait_ready():
            metabase_status = {"executed": False, "reason": "metabase did not become ready"}
        else:
            try:
                session = httpx.post(
                    f"{METABASE}/api/session",
                    json={"username": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
                    timeout=30,
                )
                if session.status_code != 200:
                    raise RuntimeError(f"login failed ({session.status_code})")
                headers = {"X-Metabase-Session": session.json()["id"]}
                cards = metabase_cards()

                per_card = {}
                for card in spec["cards"]:
                    card_id = cards.get(card["title"])
                    if card_id is None:
                        per_card[card["id"]] = {"found": False}
                        continue
                    rows = comparable(metabase_rows(card_id, headers))
                    reference = values[card["id"]]["clickhouse"]
                    per_card[card["id"]] = {
                        "found": True,
                        "metabase": rows,
                        "same": rows == reference,
                    }
                    values[card["id"]]["metabase"] = rows

                metabase_status = {"executed": True, "cards": per_card}
                executed.append("metabase")
            except Exception as exc:  # noqa: BLE001
                metabase_status = {"executed": False, "reason": f"{type(exc).__name__}: {exc}"}

    compiled = {
        "superset": "bundle rendered in Superset's import layout; not imported (stock image lacks the ClickHouse driver)",
        "looker_studio": "community connector rendered; not deployed (needs a Google account)",
        "sigma": "workbook mapping rendered; not imported (format is not public, no trial access)",
    }

    checks = {
        "sql_identical_across_all_definitions": all(item["identical"] for item in sql_checks.values()),
        "clickhouse_answered_every_card": all(values[card["id"]].get("clickhouse") is not None for card in spec["cards"]),
    }
    if metabase_status.get("executed"):
        per_card = metabase_status["cards"]
        checks["metabase_found_every_card"] = all(item.get("found") for item in per_card.values())
        checks["metabase_matches_clickhouse"] = all(item.get("same") for item in per_card.values())

    if not metabase_status.get("executed"):
        result = "CANNOT_VERIFY"
    else:
        result = "PASS" if all(checks.values()) else "FAIL"

    report = {
        "ran_at": started.isoformat(),
        "dashboard": spec["dashboard"]["name"],
        "cards": [card["id"] for card in spec["cards"]],
        "definitions": {
            target: {
                "file": path,
                "sql": sql_checks[target],
                "status": "executed" if target in executed else "compiled, not executed",
                **({"reason": compiled[target]} if target in compiled else {}),
            }
            for target, (path, _extractor, _key) in EXTRACTORS.items()
        },
        "executed_on": executed,
        "metabase": metabase_status,
        "values": values,
        "checks": checks,
        "result": result,
        "notes": [
            "The SQL is compared after whitespace normalisation; the number is compared after "
            "rounding to 6 decimals, because two JSON encoders disagree about 15 and 15.0.",
            "Compiled means the definition is rendered in the tool's own format and carries the "
            "verified SQL. It does not mean the tool ran it.",
        ],
    }

    out = Path(args.report)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print(f"cards: {len(spec['cards'])}")
    for target, item in sql_checks.items():
        state = "identical" if item["identical"] else f"different={item['different']} missing={item['missing']}"
        print(f"  {target:14s} sql {state}")
    print(f"  executed on    {', '.join(executed)}")
    if metabase_status.get("executed"):
        matched = sum(1 for item in metabase_status["cards"].values() if item.get("same"))
        print(f"  metabase       {matched}/{len(spec['cards'])} cards match clickhouse")
    else:
        print(f"  metabase       not executed: {metabase_status.get('reason')}")
    print()
    for name, ok in checks.items():
        print(f"  [{'OK  ' if ok else 'FAIL'}] {name}")
    print(f"RESULT: {result}")
    return 0 if result == "PASS" else (2 if result == "CANNOT_VERIFY" else 1)

if __name__ == "__main__":
    raise SystemExit(main())

"""Render the neutral dashboard spec into four tools' formats.

Every renderer copies `card.sql` verbatim. None of them may rewrite it: the moment a
renderer is allowed to "fix" a query for its dialect, the four dashboards stop being the
same dashboard, which is the thing this directory exists to demonstrate.

Outputs, all under `analytics/definitions/`:

    metabase/sentinel_dashboards.json   the format `analytics/metabase/import_dashboards.py` reads
    superset/sentinel_dashboard.zip     Superset's import bundle (YAML files in a zip)
    looker_studio/Code.gs               a community connector that runs the same SQL
    sigma/sentinel_workbook.json        a workbook mapping (Sigma's import format is not public)

    python -m analytics.render
    python -m analytics.render --check  # fail if any rendered file is out of date
"""

from __future__ import annotations

import argparse
import io
import json
import uuid
import zipfile
from pathlib import Path

import yaml

SPEC = Path(__file__).parent / "dashboard_spec.yml"
OUT = Path(__file__).parent / "definitions"

# Fixed namespace so re-rendering produces identical UUIDs; a random id would make every
# run look like a change and defeat `--check`.
NAMESPACE = uuid.UUID("1f0d8f0e-9a3b-4f6e-9f2a-6c1b7a5e4d33")

def load_spec(path: Path = SPEC) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))

def _uuid(name: str) -> str:
    return str(uuid.uuid5(NAMESPACE, name))

def _cards_by_id(spec: dict) -> dict[str, dict]:
    return {card["id"]: card for card in spec["cards"]}

# --------------------------------------------------------------------------------------
# Metabase — executed. Same shape the existing importer already reads.
# --------------------------------------------------------------------------------------

def render_metabase(spec: dict) -> str:
    cards = _cards_by_id(spec)
    dashboards = []
    for dashboard in spec["dashboards"]:
        dashboards.append(
            {
                "name": dashboard["name"],
                "description": dashboard.get("description", ""),
                "cards": [
                    {
                        "name": cards[card_id]["title"],
                        "display": cards[card_id].get("display", "table"),
                        "query": cards[card_id]["sql"],
                        **(
                            {
                                "visualization_settings": {
                                    "graph.dimensions": cards[card_id]["dimensions"],
                                    "graph.metrics": cards[card_id]["metrics"],
                                }
                            }
                            if cards[card_id].get("dimensions")
                            else {}
                        ),
                    }
                    for card_id in dashboard["cards"]
                ],
            }
        )
    return json.dumps(dashboards, indent=2) + "\n"

# --------------------------------------------------------------------------------------
# Superset — compiled. A zip of YAML files in the layout `superset import-dashboards`
# expects: databases/, datasets/, charts/, dashboards/, plus metadata.yaml.
# --------------------------------------------------------------------------------------

def render_superset(spec: dict) -> bytes:
    cards = _cards_by_id(spec)
    database_uuid = _uuid("database:sentinel-clickhouse")

    files: dict[str, str] = {}
    files["metadata.yaml"] = yaml.safe_dump(
        {"version": "1.0.0", "type": "Dashboard", "timestamp": "2026-10-22T00:00:00"},
        sort_keys=False,
    )
    files["databases/SENTINEL_ClickHouse.yaml"] = yaml.safe_dump(
        {
            "database_name": "SENTINEL ClickHouse",
            "uuid": database_uuid,
            "sqlalchemy_uri": "clickhousedb+connect://default:change-me-clickhouse@clickhouse:8123/sentinel",
            "cache_timeout": None,
            "expose_in_sqllab": True,
            "allow_run_async": False,
            "allow_ctas": False,
            "allow_cvas": False,
            "allow_dml": False,
            "extra": json.dumps({"metadata_params": {}, "engine_params": {}}),
        },
        sort_keys=False,
    )

    # One dataset per card would duplicate the connection; one dataset per source table with
    # a virtual dataset carrying the card SQL keeps the SQL verbatim, which is the point.
    for card_id, card in cards.items():
        files[f"datasets/{card_id}.yaml"] = yaml.safe_dump(
            {
                "table_name": card_id,
                "uuid": _uuid(f"dataset:{card_id}"),
                "database_uuid": database_uuid,
                "sql": card["sql"],
                "main_dttm_col": None,
                "default_endpoint": None,
                "offset": 0,
                "cache_timeout": None,
                "schema": "sentinel",
                "template_params": [],
                "columns": [],
                "metrics": [],
            },
            sort_keys=False,
        )
        files[f"charts/{card_id}.yaml"] = yaml.safe_dump(
            {
                "slice_name": card["title"],
                "uuid": _uuid(f"chart:{card_id}"),
                "viz_type": {"bar": "bar", "line": "line", "pie": "pie", "table": "table"}.get(
                    card.get("display", "table"), "table"
                ),
                "datasource_uuid": _uuid(f"dataset:{card_id}"),
                "params": json.dumps(
                    {
                        "datasource": f"{_uuid(f'dataset:{card_id}')}__table",
                        "viz_type": {"bar": "bar", "line": "line", "pie": "pie", "table": "table"}.get(
                            card.get("display", "table"), "table"
                        ),
                        "groupby": card.get("dimensions", []),
                        "metrics": card.get("metrics", []),
                        "row_limit": 1000,
                    },
                    sort_keys=True,
                ),
                "query_context": None,
                "description": "",
                "cache_timeout": None,
            },
            sort_keys=False,
        )

    for dashboard in spec["dashboards"]:
        files[f"dashboards/{_uuid('dashboard:' + dashboard['name'])}.yaml"] = yaml.safe_dump(
            {
                "dashboard_title": dashboard["name"],
                "uuid": _uuid("dashboard:" + dashboard["name"]),
                "description": dashboard.get("description", ""),
                "slices": [_uuid(f"chart:{card_id}") for card_id in dashboard["cards"]],
                "position": {},
                "metadata": {"default_filters": "[]", "chart_configuration": {}},
            },
            sort_keys=False,
        )

    buffer = io.BytesIO()
    # Fixed timestamps: a zip records the wall clock per entry, so a re-render would produce
    # a different file every time and `--check` would report drift that does not exist.
    fixed = (2026, 10, 22, 0, 0, 0)
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in sorted(files.items()):
            info = zipfile.ZipInfo(name, date_time=fixed)
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, content)
    return buffer.getvalue()

# --------------------------------------------------------------------------------------
# Looker Studio — compiled. A community connector is an Apps Script project; the SQL lives
# in Code.gs and is sent to ClickHouse over HTTP.
# --------------------------------------------------------------------------------------

LOOKER_CODE = '''/**
 * Looker Studio community connector for SENTINEL.
 *
 * Generated from analytics/dashboard_spec.yml. The SQL below is the card's SQL, verbatim.
 * Not executed here: deploying a community connector needs a Google account and the Apps
 * Script runtime, neither of which is available to a CI job.
 */

var CLICKHOUSE = {
  host: 'clickhouse',
  port: 8123,
  database: 'sentinel',
  user: 'default',
  password: 'change-me-clickhouse'
};

var CARDS = {cards};

function getConfig(request) {{
  return {{
    configParams: [
      {{ name: 'card', displayName: 'Card', type: 'ENUM', options: Object.keys(CARDS).map(function (id) {{
        return {{ label: CARDS[id].title, value: id }};
      }}) }}
    ],
    dateRangeRequired: false
  }};
}}

function getSchema(request) {{
  var card = CARDS[request.configParams.card || Object.keys(CARDS)[0]];
  return {{ schema: card.fields }};
}}

function getData(request) {{
  var cardId = request.configParams.card || Object.keys(CARDS)[0];
  var card = CARDS[cardId];

  var response = UrlFetchApp.fetch(
    'http://' + CLICKHOUSE.host + ':' + CLICKHOUSE.port +
      '/?database=' + CLICKHOUSE.database + '&default_format=JSON' +
      '&output_format_json_quote_64bit_integers=0',
    {{
      method: 'post',
      contentType: 'text/plain',
      headers: {{
        'X-ClickHouse-User': CLICKHOUSE.user,
        'X-ClickHouse-Key': CLICKHOUSE.password
      }},
      payload: card.sql
    }}
  );

  var payload = JSON.parse(response.getContentText());
  var rows = (payload.data || []).map(function (row) {{
    return {{ values: card.columns.map(function (name) {{ return row[name]; }}) }};
  }});

  return {{ schema: card.fields, rows: rows }};
}}
'''

def render_looker(spec: dict) -> str:
    cards = {}
    for card in spec["cards"]:
        # The connector has to declare the schema it returns. Read from the spec where the
        # card declares dimensions/metrics; the rest are derived from the column list in the
        # query, which is why the queries all use explicit column aliases.
        columns = _sql_columns(card["sql"])
        cards[card["id"]] = {
            "title": card["title"],
            "sql": " ".join(card["sql"].split()),
            "columns": columns,
            "fields": [
                {"name": column, "label": column, "dataType": _looker_type(column), "semantics": {}}
                for column in columns
            ],
        }
    return LOOKER_CODE.replace("{cards}", json.dumps(cards, indent=2))

def _split_select(select_list: str) -> list[str]:
    """Split a SELECT list on top-level commas.

    A plain `split(",")` breaks on `coalesce(a, b)` and `if(a, b, c)` and produces column
    names like `coalesce(category_name`. Tracking parenthesis depth is the whole fix.
    """
    parts: list[str] = []
    depth = 0
    current = ""
    for character in select_list:
        if character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
        if character == "," and depth == 0:
            parts.append(current)
            current = ""
            continue
        current += character
    if current.strip():
        parts.append(current)
    return parts

def _sql_columns(sql: str) -> list[str]:
    """Column names from the SELECT list, using the aliases the queries already declare."""
    flattened = " ".join(sql.split())
    start = flattened.lower().index("select") + len("select")
    end = flattened.lower().index(" from ")
    select_list = flattened[start:end]
    columns = []
    for part in _split_select(select_list):
        tokens = part.strip().split()
        columns.append(tokens[-1] if len(tokens) > 1 else tokens[0])
    return [column.strip() for column in columns]

def _looker_type(column: str) -> str:
    if column in {"products", "season_year", "seasons", "rating"}:
        return "NUMBER"
    if column in {"inventory_value", "avg_price", "avg_goals_for", "avg_goals_against", "win_rate"}:
        return "NUMBER"
    return "TEXT"

# --------------------------------------------------------------------------------------
# Sigma — compiled. Sigma's import format is not published, so this is a mapping that keeps
# the SQL and the card identity in one place. Calling it an importable workbook would be a
# claim the repository cannot check.
# --------------------------------------------------------------------------------------

def render_sigma(spec: dict) -> str:
    workbook = {
        "name": spec["dashboard"]["name"],
        "description": spec["dashboard"]["description"],
        "source": {
            "type": "warehouse-table",
            "connection": "SENTINEL ClickHouse",
            "dialect": "clickhouse",
        },
        "pages": [
            {
                "name": dashboard["name"],
                "elements": [
                    {
                        "id": card_id,
                        "title": _cards_by_id(spec)[card_id]["title"],
                        "visualization": _cards_by_id(spec)[card_id].get("display", "table"),
                        "sql": " ".join(_cards_by_id(spec)[card_id]["sql"].split()),
                    }
                    for card_id in dashboard["cards"]
                ],
            }
            for dashboard in spec["dashboards"]
        ],
        "status": {
            "rendered": True,
            "executed": False,
            "reason": "Sigma's workbook import format is not public; no trial access was used",
        },
    }
    return json.dumps(workbook, indent=2) + "\n"

# --------------------------------------------------------------------------------------

RENDERERS = {
    "metabase/sentinel_dashboards.json": ("text", render_metabase),
    "superset/sentinel_dashboard.zip": ("bytes", render_superset),
    "looker_studio/Code.gs": ("text", render_looker),
    "sigma/sentinel_workbook.json": ("text", render_sigma),
}

def render_all(spec: dict, out: Path = OUT) -> dict[str, bytes]:
    rendered: dict[str, bytes] = {}
    for name, (kind, renderer) in RENDERERS.items():
        value = renderer(spec)
        rendered[name] = value if kind == "bytes" else value.encode("utf-8")
    return rendered

def comparable(name: str, content: bytes) -> object:
    """What `--check` compares, which is not always the bytes on disk.

    The zip is the reason this function exists. Its *content* is a pure function of the
    spec, but its *compressed bytes* are not: zipfile delegates compression to zlib, and
    different zlib builds (zlib vs zlib-ng, different levels, different SIMD paths) produce
    different output for identical input. Comparing raw bytes therefore reported the Superset
    bundle as stale on GitHub's Python while it was byte-identical on the machine that
    rendered it — the check was testing the zlib build, not the definition.

    So the comparison is on content: entry names and decompressed entry bytes. That still
    catches the failure the check exists for (spec edited, definitions not re-rendered),
    and it does not fail because CI compresses differently.
    """
    if name.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            return sorted((entry, archive.read(entry)) for entry in archive.namelist())
    return content

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render the dashboard spec into four formats.")
    parser.add_argument("--check", action="store_true", help="fail if a rendered file differs")
    parser.add_argument("--out", default=str(OUT))
    args = parser.parse_args(argv)

    spec = load_spec()
    out = Path(args.out)
    rendered = render_all(spec)

    stale = []
    for name, content in rendered.items():
        target = out / name
        if args.check:
            if not target.exists() or comparable(name, target.read_bytes()) != comparable(name, content):
                stale.append(name)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        print(f"  wrote {name} ({len(content)} bytes)")

    if args.check:
        if stale:
            print("out of date: " + ", ".join(stale))
            print("run: python -m analytics.render")
            return 1
        print(f"all {len(rendered)} rendered files are up to date")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())

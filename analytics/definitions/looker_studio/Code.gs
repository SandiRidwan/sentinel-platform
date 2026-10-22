/**
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

var CARDS = {
  "inventory_by_category": {
    "title": "Inventory value by category",
    "sql": "SELECT coalesce(category_name, '(no category)') AS category_name, products, inventory_value FROM sentinel.v_inventory_by_category ORDER BY inventory_value DESC LIMIT 15",
    "columns": [
      "category_name",
      "products",
      "inventory_value"
    ],
    "fields": [
      {
        "name": "category_name",
        "label": "category_name",
        "dataType": "TEXT",
        "semantics": {}
      },
      {
        "name": "products",
        "label": "products",
        "dataType": "NUMBER",
        "semantics": {}
      },
      {
        "name": "inventory_value",
        "label": "inventory_value",
        "dataType": "NUMBER",
        "semantics": {}
      }
    ]
  },
  "products_per_category": {
    "title": "Products per category",
    "sql": "SELECT coalesce(category_name, '(no category)') AS category_name, products FROM sentinel.v_inventory_by_category ORDER BY products DESC LIMIT 20",
    "columns": [
      "category_name",
      "products"
    ],
    "fields": [
      {
        "name": "category_name",
        "label": "category_name",
        "dataType": "TEXT",
        "semantics": {}
      },
      {
        "name": "products",
        "label": "products",
        "dataType": "NUMBER",
        "semantics": {}
      }
    ]
  },
  "avg_price_by_rating": {
    "title": "Average price by rating",
    "sql": "SELECT toString(rating) AS rating, round(avg(price), 2) AS avg_price, count() AS products FROM sentinel.fct_books WHERE rating IS NOT NULL GROUP BY rating ORDER BY rating",
    "columns": [
      "rating",
      "avg_price",
      "products"
    ],
    "fields": [
      {
        "name": "rating",
        "label": "rating",
        "dataType": "NUMBER",
        "semantics": {}
      },
      {
        "name": "avg_price",
        "label": "avg_price",
        "dataType": "NUMBER",
        "semantics": {}
      },
      {
        "name": "products",
        "label": "products",
        "dataType": "NUMBER",
        "semantics": {}
      }
    ]
  },
  "stock_status": {
    "title": "Stock status",
    "sql": "SELECT if(is_in_stock, 'in stock', 'out of stock') AS status, count() AS products FROM sentinel.fct_books GROUP BY status",
    "columns": [
      "status",
      "products"
    ],
    "fields": [
      {
        "name": "status",
        "label": "status",
        "dataType": "TEXT",
        "semantics": {}
      },
      {
        "name": "products",
        "label": "products",
        "dataType": "NUMBER",
        "semantics": {}
      }
    ]
  },
  "avg_goals_by_season": {
    "title": "Average goals for by season",
    "sql": "SELECT season_year, round(avg(goals_for), 2) AS avg_goals_for, round(avg(goals_against), 2) AS avg_goals_against FROM sentinel.fct_hockey_season GROUP BY season_year ORDER BY season_year",
    "columns": [
      "season_year",
      "avg_goals_for",
      "avg_goals_against"
    ],
    "fields": [
      {
        "name": "season_year",
        "label": "season_year",
        "dataType": "NUMBER",
        "semantics": {}
      },
      {
        "name": "avg_goals_for",
        "label": "avg_goals_for",
        "dataType": "NUMBER",
        "semantics": {}
      },
      {
        "name": "avg_goals_against",
        "label": "avg_goals_against",
        "dataType": "NUMBER",
        "semantics": {}
      }
    ]
  },
  "best_win_rates": {
    "title": "Best win rates",
    "sql": "SELECT team_name, round(avg(win_rate_recomputed), 3) AS win_rate, count() AS seasons FROM sentinel.fct_hockey_season WHERE win_rate_recomputed IS NOT NULL GROUP BY team_name ORDER BY win_rate DESC LIMIT 15",
    "columns": [
      "team_name",
      "win_rate",
      "seasons"
    ],
    "fields": [
      {
        "name": "team_name",
        "label": "team_name",
        "dataType": "TEXT",
        "semantics": {}
      },
      {
        "name": "win_rate",
        "label": "win_rate",
        "dataType": "NUMBER",
        "semantics": {}
      },
      {
        "name": "seasons",
        "label": "seasons",
        "dataType": "NUMBER",
        "semantics": {}
      }
    ]
  }
};

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

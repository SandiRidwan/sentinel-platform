-- SENTINEL — ClickHouse analytical layer.
--
-- ClickHouse is here for one reason: the dashboards ask questions that aggregate the whole
-- fact table, and a column store answers those in tens of milliseconds where a row store
-- needs a scan. It is not the system of record; gold in the warehouse is.
--
-- Engine choices:
--   ReplacingMergeTree  — reloading the same day replaces rows instead of duplicating them.
--   ORDER BY            — the key the dashboard actually filters on.
--   LowCardinality      — currency, density_band and similar repeat heavily; this compresses
--                         them and speeds up GROUP BY.

CREATE DATABASE IF NOT EXISTS sentinel;

-- One row per product.
CREATE TABLE IF NOT EXISTS sentinel.fct_books
(
    book_key          String,
    category_key      Nullable(String),
    upc               String,
    currency          LowCardinality(String),
    price             Nullable(Float64),
    stock_count       Nullable(Int64),
    rating            Nullable(Int32),
    is_in_stock       Bool,
    inventory_value   Float64,
    is_rated          UInt8,
    loaded_at         DateTime DEFAULT now()
)
ENGINE = ReplacingMergeTree(loaded_at)
ORDER BY (book_key)
SETTINGS index_granularity = 8192;

-- Category dimension, small and read in full.
CREATE TABLE IF NOT EXISTS sentinel.dim_category
(
    category_key   String,
    category_slug  String,
    category_name  String,
    category_url   Nullable(String),
    loaded_at      DateTime DEFAULT now()
)
ENGINE = ReplacingMergeTree(loaded_at)
ORDER BY (category_key);

-- Country dimension.
CREATE TABLE IF NOT EXISTS sentinel.dim_country
(
    country_key         String,
    country_name        String,
    capital_name        Nullable(String),
    population          Nullable(Int64),
    area_km2            Nullable(Float64),
    population_density  Nullable(Float64),
    density_band        LowCardinality(String),
    loaded_at           DateTime DEFAULT now()
)
ENGINE = ReplacingMergeTree(loaded_at)
ORDER BY (country_key);

-- Hockey seasons, keyed by season then team so a season-range filter is a range scan.
CREATE TABLE IF NOT EXISTS sentinel.fct_hockey_season
(
    season_key            String,
    season_year           Int32,
    team_name             String,
    wins                  Nullable(Int32),
    losses                Nullable(Int32),
    ot_losses             Nullable(Int32),
    win_pct               Nullable(Float64),
    goals_for             Nullable(Int32),
    goals_against         Nullable(Int32),
    goal_difference       Nullable(Int32),
    games_played          Nullable(Int64),
    win_rate_recomputed   Nullable(Float64),
    loaded_at             DateTime DEFAULT now()
)
ENGINE = ReplacingMergeTree(loaded_at)
ORDER BY (season_year, team_name);

-- Convenience view for the dashboard: inventory value by category.
CREATE VIEW IF NOT EXISTS sentinel.v_inventory_by_category AS
SELECT
    c.category_name                        AS category_name,
    count()                                AS products,
    round(sum(f.inventory_value), 2)       AS inventory_value,
    round(avg(f.price), 2)                 AS avg_price,
    round(avg(f.rating), 2)                AS avg_rating
FROM sentinel.fct_books AS f
LEFT JOIN sentinel.dim_category AS c ON f.category_key = c.category_key
GROUP BY c.category_name
ORDER BY inventory_value DESC;

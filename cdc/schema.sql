-- SENTINEL — the operational table the CDC path captures.
--
-- This is deliberately a *source system* table, not a warehouse table: it is what an
-- application would write to, and Debezium reads its write-ahead log rather than
-- querying it. Nothing here knows that SENTINEL exists.
--
-- Applied by cdc/seed.py so it is idempotent on an existing Postgres volume; a
-- docker-entrypoint-initdb.d script would only run once, on a fresh volume.

CREATE SCHEMA IF NOT EXISTS ops;

CREATE TABLE IF NOT EXISTS ops.product_price (
    product_id    TEXT            PRIMARY KEY,
    product_name  TEXT            NOT NULL,
    category      TEXT            NOT NULL,
    price         NUMERIC(12, 2)  NOT NULL,
    currency      CHAR(3)         NOT NULL,
    in_stock      BOOLEAN         NOT NULL,
    updated_at    TIMESTAMPTZ     NOT NULL DEFAULT now()
);

-- REPLICA IDENTITY FULL is not needed while the primary key is the replica identity:
-- Debezium then publishes the key for UPDATE/DELETE, which is all the consumer needs
-- to apply the change to a keyed table. Setting FULL would ship the entire before-image
-- on every update and multiply WAL volume for no gain.
ALTER TABLE ops.product_price REPLICA IDENTITY DEFAULT;

-- The publication is what logical decoding reads. Creating it explicitly (rather than
-- letting Debezium create one) keeps the captured table list reviewable in SQL, which
-- is the same reasoning as governance/sources.yml: the decision is written down.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_publication WHERE pubname = 'sentinel_cdc') THEN
        CREATE PUBLICATION sentinel_cdc FOR TABLE ops.product_price;
    END IF;
END
$$;

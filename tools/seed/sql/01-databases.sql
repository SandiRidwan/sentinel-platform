-- SENTINEL — Postgres bootstrap.
-- Runs on first container init (docker-entrypoint-initdb.d). For an already-running
-- volume, apply manually:  docker compose exec -T postgres psql -U sentinel -d sentinel -f -
--
-- A separate database is needed because Metabase stores its own application schema
-- and must not share the `sentinel` database that holds our warehouse objects.

SELECT 'CREATE DATABASE metabase'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'metabase')\gexec

-- Warehouse schemas inside the sentinel database (dev target).
CREATE SCHEMA IF NOT EXISTS bronze;
CREATE SCHEMA IF NOT EXISTS silver;
CREATE SCHEMA IF NOT EXISTS gold;
CREATE SCHEMA IF NOT EXISTS governance;

-- Audit table: every ingestion run writes one row PER SPIDER. The primary key is
-- (run_id, spider), not run_id alone — a single run covers several spiders, and a
-- run_id-only key silently discards every spider after the first.
CREATE TABLE IF NOT EXISTS governance.ingest_run (
    run_id          UUID        NOT NULL,
    spider          TEXT        NOT NULL,
    source          TEXT        NOT NULL,
    started_at      TIMESTAMPTZ NOT NULL,
    finished_at     TIMESTAMPTZ,
    status          TEXT        NOT NULL DEFAULT 'running',
    rows_landed     BIGINT      NOT NULL DEFAULT 0,
    rows_rejected   BIGINT      NOT NULL DEFAULT 0,
    error           TEXT,
    PRIMARY KEY (run_id, spider),
    CONSTRAINT ingest_run_status_check CHECK (status IN ('running', 'success', 'failed'))
);

CREATE INDEX IF NOT EXISTS ingest_run_source_idx ON governance.ingest_run (source, started_at DESC);

-- Counters emitted by each pipeline stage so dashboards do not have to scan fact tables.
CREATE TABLE IF NOT EXISTS governance.stage_metric (
    run_id      UUID        NOT NULL,
    stage       TEXT        NOT NULL,
    metric      TEXT        NOT NULL,
    value       DOUBLE PRECISION NOT NULL,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, stage, metric)
);

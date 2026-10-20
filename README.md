# SENTINEL — Governed Web-Data Platform

**Ingests bot-defended public sources, lands them in a lakehouse, publishes governed
analytics-ready data, and runs itself.**

```
Scrapy + Go  ->  Airflow  ->  Iceberg/S3  ->  PySpark + dbt  ->  BigQuery / Snowflake
                                        |
                      ClickHouse + Metabase   (analytics)
                      OpenLineage + PII       (governance)
                      Terraform + Kubernetes  (infra)
                      pytest + contracts + GE (quality)
```

| | |
|---|---|
| **Why** | Scraping is easy to start and hard to operate. Data that arrives wrong is worse than no data. |
| **Goal** | One platform where extraction, orchestration, transformation, quality, governance and infra are all visible and testable. |
| **Impact** | Every number in this repo is measured from a real run. Nothing is estimated. |

## Status

| Milestone | Component | State |
|---|---|---|
| M1 | Skeleton, docker-compose, MinIO, task runner | see `make doctor` |
| M2 | Scrapy spiders + contract tests | — |
| M3 | Go fetcher service | — |
| M4 | Airflow DAGs | — |
| M5 | PySpark + Iceberg | — |
| M6 | dbt (dual warehouse) | — |
| M7 | Quality suite (4 levels) + CI gate | — |
| M8 | ClickHouse + Metabase | — |
| M9 | Governance (PII, lineage, sources gate) | — |
| M10 | Terraform + Kubernetes | — |
| M11 | Observability | — |
| M12 | ADRs, RFC, runbooks, mentoring | — |

> This table is updated at the end of every milestone. A milestone is only marked done when
> its verification command passes. No partial credit.

## Quickstart

Requires: Python 3.12+, Docker + Compose, Go 1.22+, Terraform 1.6+, make.

```bash
git clone https://github.com/SandiRidwan/sentinel-platform
cd sentinel-platform
cp .env.example .env

make doctor      # check every tool the platform needs
make up          # start the local stack (MinIO, Postgres, ClickHouse, Metabase)
make seed        # create buckets, databases, topics
make scrape      # run the Scrapy spiders against the sandbox sources
make quality     # run all four quality levels
make down        # stop everything
```

On Windows without `make` on PATH, use the cross-platform runner:

```powershell
.\tasks.ps1 doctor
.\tasks.ps1 up
.\tasks.ps1 quality
```

Both entries call the same implementation in `tools/tasks/`.

## Sources

Every source is declared in `governance/sources.yml` with its robots.txt status, terms-of-service
position and legal basis. **Ingestion refuses to start for an undeclared source.** The default
set targets sandbox sites that are published for exactly this purpose:

| Source | Why it is safe | robots.txt |
|---|---|---|
| `books.toscrape.com` | Built by Scrapinghub as a scraping sandbox | allow all |
| `quotes.toscrape.com` | Built as a scraping sandbox | allow all |
| `scrapethissite.com` | Built as a scraping sandbox | allow all |
| `en.wikipedia.org` (API only) | Official API, rate-limited, licensed CC BY-SA | API permitted |

No target is scraped that has not been checked. See `docs/adr/` for the decision record.

## Layout

```
ingestion/scrapy/       spiders + contracts + middlewares + pipelines
ingestion/go-fetcher/   Go HTTP service: TLS impersonation, worker pool, rate limit
orchestration/dags/     Airflow DAGs (ingest, transform, quality gate)
transform/spark/        PySpark jobs (raw -> bronze -> silver)
transform/dbt/          dbt models + tests + snapshots (BigQuery, Snowflake, DuckDB)
analytics/              ClickHouse DDL + Metabase dashboards as code
governance/             sources.yml gate, PII detection/masking, lineage, policies
quality/                unit / contract / warehouse / gate
infra/terraform/        cloud modules with a written cost ceiling
infra/k8s/              Helm charts + local kind deployment
observability/          OpenTelemetry, Prometheus rules, Grafana dashboards
docs/                   ADRs, RFC, runbooks, CODE_REVIEW, MENTORING
tools/tasks/            the task runner shared by make and tasks.ps1
```

## Local vs cloud

The whole pipeline runs locally with no cloud spend: MinIO stands in for S3, DuckDB/Postgres for
the warehouse, and `kind` for Kubernetes. The cloud path (BigQuery, Snowflake, EMR, MWAA) is
fully written and exercised once, then torn down with `terraform destroy`. The cost ceiling is
written down in `infra/terraform/README.md`.

## Licence

MIT. See `LICENSE`.

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
| **Why** | Scraping is easy to start and hard to operate. Data that arrives wrong is worse than no data, and a pipeline that reports success while producing nothing is the failure nobody catches. |
| **Goal** | One platform where extraction, orchestration, transformation, quality, governance and infrastructure are all visible, testable and enforced. |
| **Impact** | Every number below came from a command in this repository. Nothing is estimated. |

## Status — all twelve milestones complete

| Milestone | What it delivers | Verified by |
|---|---|---|
| M1 | Skeleton, local stack, one task runner for make and PowerShell | `make doctor` → all tools and 4 services reachable |
| M2 | 5 Scrapy spiders, source gate, Parquet sink | 732 items landed, 0 errors |
| M3 | Go fetcher: utls fingerprint, bounded pool, token bucket | 5/5 endpoint checks; benchmark 32,755 ns/op |
| M4 | Airflow: per-source ingest, transform, quality gate | 7 DAGs parse, 0 import errors; `gate_check` SUCCESS |
| M5 | PySpark bronze/silver + Iceberg | 707 silver rows; time travel 57 → 114 |
| M6 | dbt: 3 dims, 2 facts, snapshot, 30 tests | `dbt build` PASS=41 ERROR=0 |
| M7 | Four-level quality suite + CI gate | `make quality` → all four PASS |
| M8 | ClickHouse + Metabase dashboards as code | 3 dashboards; cards return 15/5/10 rows |
| M9 | Lineage, PII scan, column-level security | 14 edges; 5 columns clean; 9 policies validated |
| M10 | Terraform + Kubernetes | plan exit 0 offline; kind deploy, 3 endpoints 200 |
| M11 | Metrics, alert rules, dashboard, OTel | every rule metric exists in live `/metrics` |
| M12 | 10 ADRs, 1 RFC, 4 runbooks, review + mentoring | this directory |

## Measured

| Number | Value | Command |
|---|---|---|
| Raw items landed | 732 across 5 spiders, 0 errors | `make scrape` |
| Silver rows | 707 (books 57, categories 50, quotes 100, countries 250, hockey 250) | `python -m transform.spark.bronze_to_silver` |
| dbt models + tests | 41 checks, 0 failures, 0 warnings | `make docs` / `python -m tools.tasks.dbt build` |
| Unit tests | 21 | `make test` |
| Spider contracts | 5/5 spiders pass | `make quality` |
| Expectation suites | 4/4 tables, 4+3+3 expectations | `make quality` |
| Go throughput | 134,546 iterations, 32,755 ns/op (~30,500 rps local) | `go test -bench=BenchmarkFetch` |
| Iceberg time travel | snapshot 1 = 57 rows, snapshot 2 = 114 rows | `python -m transform.spark.iceberg_publish --time-travel books` |
| Lineage edges | 14 | `make lineage` |
| Terraform plan | exit 0, 0 resources, no credentials | `terraform plan -var-file=dev.tfvars` |

## Quickstart

Requires: Python 3.11+, Docker + Compose, Go 1.22+, Terraform 1.6+, make (or PowerShell).

```bash
git clone https://github.com/SandiRidwan/sentinel-platform
cd sentinel-platform
cp .env.example .env

make doctor      # every tool and service the platform needs
make up          # S3, Postgres, ClickHouse, Metabase, Airflow, go-fetcher
make seed        # buckets and database objects
make scrape      # 5 spiders against declared sandbox sources
make quality     # four levels; fails the run if any level regresses
```

Windows without `make` on PATH: `.\tasks.ps1 doctor` and so on. Both entry points call
`tools/tasks/runner.py`, so they cannot drift apart.

Full pipeline, in order:

```bash
make up && make seed && make scrape
python -m transform.spark.bronze_to_silver     # raw -> bronze -> silver (needs JDK 17)
python -m tools.tasks.dbt build                 # silver -> gold, 41 checks
make clickhouse-load && make metabase-import    # serving layer + dashboards
make quality && make lineage && make pii-scan
```

## Sources — the gate

Every host must be declared in `governance/sources.yml` with its robots.txt status and legal
basis. **Ingestion refuses an undeclared host before making a request.**

| Source | Why it is safe | robots.txt |
|---|---|---|
| `books.toscrape.com` | Published by Zyte as a scraping sandbox | allow all |
| `quotes.toscrape.com` | Published as a scraping sandbox | allow all |
| `scrapethissite.com` | Published for scraping exercises | only `/lessons/` and `/faq/` disallowed |

Refused, and recorded as refused: `en.wikipedia.org` (robots.txt sets `Disallow: /w/`, which
covers `/w/api.php` — my first draft marked it `api_permitted` and was wrong),
`openlibrary.org` (`/api` and `/search` disallowed), plus LinkedIn, Facebook and Twitter on
terms-of-service grounds.

The Wikipedia case is the point of the gate: the honest outcome was to remove the source, not
to carve an exception.

## Layout

```
ingestion/scrapy/       spiders + contracts + gate + Parquet sink
ingestion/go-fetcher/   Go service: utls ClientHello, worker pool, token bucket
orchestration/dags/     Airflow: ingest per source, transform, quality gate
transform/spark/        PySpark bronze/silver + Iceberg publish (+ pandas fallback)
transform/dbt/          models, tests, snapshot, dual warehouse
analytics/              ClickHouse DDL + loader, Metabase dashboards as code
governance/             sources gate, PII scan, column security, lineage
quality/                L1 unit, L2 contracts, L3 warehouse, L4 expectations
infra/terraform/        lake + BigQuery + budget (Snowflake is its own stack)
infra/k8s/              kind cluster + plain manifests
observability/          Prometheus rules, Grafana dashboard, OTel collector
docs/                   10 ADRs, 1 RFC, 4 runbooks, CODE_REVIEW, MENTORING
```

## Techniques worth naming

- **Schema on write, not on read.** Every Parquet file is written against a declared Arrow
  schema. An all-null column does not become a string in one flush and an int64 in the next.
  That exact drift made Spark refuse to read a table.
- **A gate that runs first.** `gate_check` is the first Airflow task and the first middleware;
  an unapproved source costs zero requests.
- **Validation that can fail the run.** Four levels, and the gate DAG raises when any level
  fails. `L4` used to report `PASS` over an entirely skipped set; that is now a failure.
- **Identity separated from content.** `governance/policies/column_security.yml` classifies
  identity and free-text separately, and a consumer view exposes one or the other.
- **Offline plan.** `terraform plan` exits 0 with no credentials, so CI can validate
  infrastructure from a fork.
- **Two engines, one result.** The Spark and pandas paths produce identical row counts, which
  is what makes the fallback safe rather than a second implementation with its own bugs.

## Limitations

Stated because a reviewer who finds an unstated one stops trusting the stated ones.

- **Scale is local.** Hundreds of thousands of rows, hundreds of requests per minute. The
  warehouse work is real; the volume is not enterprise.
- **s3mock is a test double.** MinIO withdrew its public images in 2026. The application only
  knows `S3_ENDPOINT_URL`, so production is a config change, but s3mock is not a system of
  record.
- **Iceberg history is two snapshots deep** in this demo. The mechanism is verified; a real
  retention policy over months is not.
- **The Go service speaks HTTP/1.1 over the impersonated TLS connection.** utls ignores
  `Config.NextProtos` when a ClientHelloID is set, so ALPN is narrowed on the spec. The rest
  of the ClientHello matches a real browser; the ALPN extension does not. `x/net/http2` on the
  same dialer is the fix, and is not done.
- **`docs/rfc/0001` is a proposal, not a feature.** Block-level deduplication is not built.
- **No SLOs.** There is no traffic to define one against.

## Licence

MIT. See `LICENSE`.

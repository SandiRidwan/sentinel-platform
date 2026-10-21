<div align="center">

<img src="https://readme-typing-svg.demolab.com?font=Orbitron&weight=900&size=34&duration=3000&pause=1000&color=6D28D9&center=true&vCenter=true&width=940&height=70&lines=SENTINEL+PLATFORM;Scrape+%E2%86%92+Govern+%E2%86%92+Serve" alt="SENTINEL Platform" />

**A governed web-data platform. It scrapes sites that fight back, and it refuses to lie about it.**

![Python](https://img.shields.io/badge/Python-3.11-1F5C3D?style=for-the-badge&logo=python&logoColor=white)
![Go](https://img.shields.io/badge/Go-1.22-00ADD8?style=for-the-badge&logo=go&logoColor=white)
![Airflow](https://img.shields.io/badge/Airflow-2.10-017CEE?style=for-the-badge&logo=apacheairflow&logoColor=white)
![dbt](https://img.shields.io/badge/dbt-1.9-FF694B?style=for-the-badge&logo=dbt&logoColor=white)
![Spark](https://img.shields.io/badge/PySpark-3.5-E25A1C?style=for-the-badge&logo=apachespark&logoColor=white)
![Iceberg](https://img.shields.io/badge/Iceberg-v2-1D4ED8?style=for-the-badge)
![ClickHouse](https://img.shields.io/badge/ClickHouse-25.4-FFCC01?style=for-the-badge&logo=clickhouse&logoColor=black)
![Terraform](https://img.shields.io/badge/Terraform-1.9-7B42BC?style=for-the-badge&logo=terraform&logoColor=white)
![Kubernetes](https://img.shields.io/badge/Kubernetes-kind-326CE5?style=for-the-badge&logo=kubernetes&logoColor=white)

![Milestones](https://img.shields.io/badge/Milestones-12%2F12-2E7D32?style=for-the-badge)
![Quality](https://img.shields.io/badge/Quality-4_levels-6D28D9?style=for-the-badge)
![License](https://img.shields.io/badge/License-MIT-0D1117?style=for-the-badge)

</div>

---

## 🏛️ What this is

Eleven components that turn bot-defended public pages into governed, analytics-ready tables —
with a **gate that refuses an unapproved source before it makes a request**, and a **quality
suite that fails the pipeline when a number stops making sense**.

Most scraping projects stop at "it returned data". This one continues to the part that is
actually hard: proving the data is still right a week later, and being able to say what the
platform is allowed to touch and why.

> **Why** — scraping is easy to start and hard to operate. A pipeline that reports success
> while producing nothing is the failure nobody catches.
> **Goal** — make every layer visible, testable and enforced: extraction, orchestration,
> transformation, quality, governance, infrastructure.
> **Impact** — **732** raw items, **707** silver rows, **41** dbt checks, **0** failures.
> Every number below came from a command in this repo.

```bash
make up && make seed && make scrape      # 732 items, 0 errors
python -m transform.spark.bronze_to_silver   # 707 silver rows
python -m tools.tasks.dbt build              # PASS=41  ERROR=0
make quality                                 # 4/4 levels pass
```

---

## 📐 The pipeline

```
   ┌──────────────┐
   │  sources.yml │  the gate: no declaration, no request
   └──────┬───────┘
          ▼
   ┌──────────────┐        ┌───────────────┐
   │ Scrapy       │        │ Go fetcher    │   utls ClientHello · worker pool
   │ 5 spiders    │        │ /fetch        │   token bucket · rate limiter
   └──────┬───────┘        └───────┬───────┘
          └────────┬───────────────┘
                   ▼
          ┌─────────────────┐
          │ raw  (S3)       │  Parquet · partitioned dt=/hour= · declared schema
          └────────┬────────┘
                   ▼   Airflow
          ┌─────────────────┐
          │ bronze → silver │  PySpark · Iceberg v2 · deduplicated
          └────────┬────────┘
                   ▼   dbt
          ┌─────────────────┐
          │ gold            │  3 dims · 2 facts · snapshot · 30 tests
          └────┬───────┬────┘
               ▼       ▼
        ClickHouse   BigQuery / Snowflake
               ▼
          Metabase (dashboards as code)
```

---

## 📊 Measured results

| Stage | Result | Command |
|---|---|---|
| Raw ingestion | **732** items · 5 spiders · 0 errors | `make scrape` |
| Silver transform | **707** rows · 16.5 s | `python -m transform.spark.bronze_to_silver` |
| dbt models + tests | **41** checks · 0 failures · 0 warnings | `python -m tools.tasks.dbt build` |
| Unit tests | **21** | `make test` |
| Spider contracts | **5/5** | `make quality` |
| Expectation suites | **4/4** tables | `make quality` |
| Go throughput | **134,546** iterations · **32,755 ns/op** | `go test -bennchmarkFetch` |
| Iceberg time travel | snapshot 1 = **57** rows → snapshot 2 = **114** | `--time-travel books` |
| Lineage edges | **14** | `make lineage` |
| Terraform plan | exit **0** · 0 resources · no credentials | `terraform plan` |

Per-table silver rows:

| Table | Rows | Dedup key | Dropped |
|---|---:|---|---:|
| `books` | 57 | `upc` | 0 |
| `book_categories` | 50 | `slug` | 0 |
| `quotes` | 100 | `author + text` | 0 |
| `countries` | 250 | `name` | 0 |
| `hockey_teams` | 250 | `year + team` | 25 |

---

## 🧩 The eleven components

| # | Component | What it does | Verified by |
|---|---|---|---|
| 1 | **Source gate** | Refuses any host not declared with a legal basis | 21 unit tests |
| 2 | **Scrapy layer** | 5 spiders, contract tests, PII masking, Parquet sink | 732 items |
| 3 | **Go fetcher** | utls ClientHello, bounded pool, token bucket, `/metrics` | 5/5 endpoint checks |
| 4 | **Airflow** | Per-source DAGs, transform, quality gate, SLA + retry | 7 DAGs, 0 import errors |
| 5 | **PySpark** | raw → bronze → silver with declared schemas | 707 rows |
| 6 | **Iceberg** | Table format with snapshots and time travel | 57 → 114 rows |
| 7 | **dbt** | 3 dims, 2 facts, Type-2 SCD snapshot, dual warehouse | PASS=41 |
| 8 | **Quality** | Four levels, gate fails the run | 4/4 pass |
| 9 | **ClickHouse + Metabase** | Analytical serving + dashboards as code | 15/5/10 rows per card |
| 10 | **Governance** | PII scan, column security, OpenLineage | 9 policies, 14 edges |
| 11 | **Terraform + K8s** | Lake, warehouse, budget; real kind deploy | plan exit 0, 3 endpoints 200 |

---

## ⚡ Engineering problems solved

Fifteen real failures, each found by running the thing rather than assuming it worked.
The error text is quoted because it is the only proof the problem was real.

### Challenge 1 — The two writers that disagreed about a column

Spark and pandas both wrote silver into the same directory. Spark omitted `_ingested_from`;
pandas included it. DuckDB takes the schema from whichever file it opens first.

```
Binder Error: Referenced column "_ingested_from" not found in FROM clause!
```

**Fix:** the Spark path now emits the lineage column too. Then a second bug on top —
`select()` dropped the column before `withColumn` could reference it, so it had to become
part of the select list.

### Challenge 2 — Schema drift made a table unreadable

A column that happened to be all-null was written as `string` by one flush and `int64` by
the next. Spark refused the entire table.

```
SchemaColumnConvertNotSupportedException: column: [ot_losses]
```

**Fix:** explicit per-item Arrow schemas. Schema on write, never inferred per batch.

### Challenge 3 — utls negotiated HTTP/2 into a transport that speaks HTTP/1.1

`utls.UClient` with a `ClientHelloID` ignores `Config.NextProtos` and offers `h2`. Go's
transport reads the server's SETTINGS frame as garbage.

```
net/http: HTTP/1.x transport connection broken:
malformed HTTP response "\x00\x00\x12\x04\x00..."
```

**Fix:** narrow ALPN on the ClientHello **spec** (`fingerprint.SpecHTTP11`). Documented
cost: the ALPN extension no longer matches a real Chrome; the rest of the hello does.

### Challenge 4 — Spark 3.5 cannot run on JDK 25

```
java.lang.UnsupportedOperationException: getSubject is not supported
```

Hadoop's `UserGroupInformation` calls `Subject.getSubject`, removed in JDK 24.
**Fix:** `session.py` selects a supported JDK (8/11/17) and reports when it cannot. A pandas
fallback produces **identical** tables — 707 rows either way.

### Challenge 5 — Spark on Windows dies without winutils

```
UnsatisfiedLinkError: 'boolean org.apache.hadoop.io.nativeio.NativeIO$Windows.access0(...)'
```

**Fix:** `HADOOP_HOME` detected automatically; winutils documented.

### Challenge 6 — Scrapy 2.13 against Twisted 26

```
ImportError: cannot import name '_setAcceptableProtocols' from 'twisted.internet._sslverify'
```

**Fix:** pin Twisted 24.11.0, with the reason written next to the pin.

### Challenge 7 — Every spider contract failed

```
TypeError: BooksSpider.parse_book() missing 2 required positional arguments
```

`scrapy check` calls the callback with only the response.
**Fix:** defaults on `rating_word` and `category`.

### Challenge 8 — A primary key that silently discarded data

`governance.ingest_run` had `PRIMARY KEY (run_id)`. A run covers several spiders, so every
spider after the first hit `ON CONFLICT DO NOTHING` and vanished. The log said `rows=2`
while the table held one.

**Fix:** `PRIMARY KEY (run_id, spider)`, plus a migration.

### Challenge 9 — A staging cache that served yesterday's data

`download_prefix` skipped files that already existed at the destination. A re-scrape was
ignored, and the warehouse kept showing old rows **with no error anywhere**.
**Fix:** always re-download.

### Challenge 10 — Every book had a NULL category

The spider read the category from the catalogue page breadcrumb — where the third item is
`Page N of 50` with no link. The selector never matched, so all 57 rows were null and the
join "worked" by returning one empty group.
**Fix:** read it from the detail page. 57/57 populated, 36/57 match a dimension row.

### Challenge 11 — A dbt view baked in a relative path

The silver path resolved only while the working directory happened to be the dbt project.
**Fix:** the runner passes an absolute path as a var.

### Challenge 12 — A quality suite that passed while verifying nothing

L4 reported `PASS` when every table had been skipped. A suite that ran nothing has verified
nothing.
**Fix:** zero verified tables is now a failure — the exact lie that level exists to prevent.

### Challenge 13 — Postgres auth failed with the correct password

A native Postgres listened on `::` (IPv6); Docker published on `0.0.0.0`. `localhost`
resolved to `::1` first, so the client reached a server that had never heard of that user.
**Fix:** host port 5433. The ambiguity is removed, not worked around.

### Challenge 14 — Metabase crash-looped in Kubernetes

```
CrashLoopBackOff — Hmm, we couldn't connect to the database
```

Compose created the `metabase` database from `tools/seed/sql`; Kubernetes had no equivalent.
**Fix:** an `initContainer` that waits for Postgres and creates the database idempotently.

### Challenge 15 — Terraform planned against a provider it was not using

`terraform plan` tried to authenticate to a placeholder Snowflake account and to the EC2
metadata endpoint, on a machine creating nothing.

```
Error: 260000: account is empty
no EC2 IMDS role found ... dial tcp 169.254.169.254:80
```

**Fix:** Snowflake moved to its own stack; AWS skip-flags set when cloud resources are
disabled. The plan now exits 0 with no credentials — which is what CI needs.

---

## 🛡️ The governance gate

Every host must be declared in `governance/sources.yml` with its robots status and legal
basis. An undeclared host costs **zero HTTP requests**.

| Source | Why it is safe | robots.txt |
|---|---|---|
| `books.toscrape.com` | Published by Zyte as a scraping sandbox | allow all |
| `quotes.toscrape.com` | Published as a scraping sandbox | allow all |
| `scrapethissite.com` | Published for scraping exercises | only `/lessons/`, `/faq/` disallowed |

**Refused, and recorded as refused:**

| Host | Reason |
|---|---|
| `en.wikipedia.org` | robots.txt sets `Disallow: /w/` — covers `/w/api.php` |
| `openlibrary.org` | `/api` and `/search` disallowed |
| `linkedin.com`, `facebook.com`, `twitter.com` | Terms of Service prohibit collection |

> The Wikipedia case is the point of the gate. My first draft marked it `api_permitted` and
> was **wrong**. The honest outcome was to remove the source, not to carve an exception —
> because the value of obeying robots.txt is that it is not carved up when a source is
> convenient.

---

## ✅ Quality — four levels, and the gate fails the run

```
L1  unit (pytest)                 21 tests
L2  spider contracts              5/5 spiders
L3  warehouse                     dbt test + column contracts + row volume + freshness
L4  expectations                  4/4 tables
```

```
$ make quality
OK L1 unit (pytest) passed
OK L2 contract (scrapy check) passed
OK L3 warehouse (dbt test) passed
OK L4 expectations passed
OK all quality levels passed
```

L3 asserts **real row counts**, not "the table exists":

```
dim_book             PASS  rows=57
dim_category         PASS  rows=50
dim_country          PASS  rows=250
fct_books            PASS  rows=57
fct_hockey_season    PASS  rows=250
```

The tests that matter are the ones that could silently pass over nothing — grain
uniqueness, column types, volume, and a suite that fails when it verifies nothing.

---

## 📁 Structure

```
sentinel-platform/
├── ingestion/
│   ├── scrapy/                 spiders · contracts · gate · PII mask · Parquet sink
│   └── go-fetcher/             utls ClientHello · worker pool · token bucket · /metrics
├── orchestration/dags/         ingest per source · transform · quality gate
├── transform/
│   ├── spark/                  bronze/silver · Iceberg publish · pandas fallback
│   └── dbt/                    5 staging · 5 marts · snapshot · 30 tests
├── analytics/
│   ├── clickhouse/             DDL · loader
│   └── metabase/               3 dashboards as JSON · idempotent importer
├── governance/
│   ├── sources.yml             the gate
│   ├── pii/                    independent scan of what landed
│   ├── policies/               column security · 9 entries · consumer views
│   └── lineage/                OpenLineage events · Mermaid graph
├── quality/
│   ├── unit/  contract/  warehouse/  expectations/     four levels
│   └── reports/                generated, gitignored
├── infra/
│   ├── terraform/              lake · BigQuery · budget   (snowflake/ separate)
│   └── k8s/                    kind config · 5 manifests
├── observability/              prometheus rules · grafana dashboard · OTel collector
├── docs/
│   ├── adr/                    10 architecture decision records
│   ├── rfc/                    1 RFC (proposed, not built)
│   ├── runbooks/               4 incident runbooks
│   ├── CODE_REVIEW.md
│   └── MENTORING.md
├── tools/
│   ├── common/                 config · logging · s3 · sources
│   ├── tasks/                  runner · dbt · k8s
│   ├── lineage.py
│   └── seed/
├── Makefile                    canonical entry
└── tasks.ps1                   same commands, Windows
```

---

## 🚀 Quick Start

```bash
git clone https://github.com/SandiRidwan/sentinel-platform
cd sentinel-platform
cp .env.example .env

make doctor      # every tool and service, with versions
make up          # S3 · Postgres · ClickHouse · Metabase · Airflow · go-fetcher
make seed        # buckets and database objects
make scrape      # 5 spiders against declared sources
make quality     # four levels
```

Windows without `make` on PATH:

```powershell
.\tasks.ps1 doctor
.\tasks.ps1 up
.\tasks.ps1 quality
```

Both entry points call `tools/tasks/runner.py`, so they cannot drift apart.

Full pipeline:

```bash
make up && make seed && make scrape
python -m transform.spark.bronze_to_silver      # needs JDK 17
python -m tools.tasks.dbt build
make clickhouse-load && make metabase-import
make quality && make lineage && make pii-scan
```

Kubernetes instead of compose:

```bash
docker compose down          # both publish 8088/8123/3000
make k8s-up
curl localhost:8088/healthz
make k8s-down
```

---

## 🛠️ Tech Stack

| Layer | Technology |
|---|---|
| **Ingestion** | Scrapy 2.13 · utls (JA3/JA4 ClientHello) · curl_cffi · Playwright · mitmproxy |
| **Service** | Go 1.22 · stdlib HTTP · hand-rolled token bucket · Prometheus text exposition |
| **Orchestration** | Airflow 2.10 · TaskFlow · dynamic task mapping · ExternalTaskSensor · SLA callbacks |
| **Transform** | PySpark 3.5 · Apache Iceberg v2 · pandas fallback |
| **Modeling** | dbt 1.9 · star schema · Type-2 SCD snapshot · custom generic test · dual target |
| **Warehouse** | DuckDB (dev) · BigQuery · Snowflake — one set of models |
| **Serving** | ClickHouse 25.4 (ReplacingMergeTree) · Metabase (dashboards as code) |
| **Quality** | pytest · Scrapy contracts · dbt tests · Great Expectations · CI gate |
| **Governance** | Source register · PII masking + independent scan · column-level security · OpenLineage |
| **Infra** | Terraform (S3 lifecycle · BigQuery · budget) · Kubernetes (kind) · Docker Compose |
| **Observability** | Prometheus rules · Grafana · OpenTelemetry collector |

---

## ⚠️ Limitations (honest)

- **Scale is local.** Hundreds of thousands of rows, hundreds of requests per minute. The
  warehouse work is real; the volume is not enterprise.
- **s3mock is a test double.** MinIO withdrew its public images in 2026. The application only
  knows `S3_ENDPOINT_URL`, so production is a config change — but s3mock is not a system of
  record.
- **Iceberg history is two snapshots deep.** The mechanism is verified; a retention policy
  over months is not.
- **The Go service speaks HTTP/1.1 over the impersonated TLS connection.** utls ignores
  `Config.NextProtos` when a ClientHelloID is set, so ALPN is narrowed on the spec. The rest
  of the ClientHello matches a real browser; the ALPN extension does not. `x/net/http2` on
  the same dialer is the fix, and is not done.
- **`docs/rfc/0001` is a proposal, not a feature.** Block-level deduplication is not built.
  The raw zone still grows linearly with runs; silver deduplicates afterwards.
- **No SLOs.** There is no traffic to define one against.
- **The sandbox sources are sandboxes.** The ingestion is real; the targets are published for
  exactly this purpose, which is why the gate approves them and refuses Wikipedia.

---

## 👤 Author

<div align="center">

**Sandi Ridwan** — Data Automation Engineer · Web Scraping · Data Engineering

📍 Palu, Central Sulawesi, Indonesia

[![Upwork](https://img.shields.io/badge/Upwork-Hire_Me-1D4ED8?style=for-the-badge&logo=upwork&logoColor=white)](https://www.upwork.com/freelancers/~011f6d0fbb4a372974)
[![LinkedIn](https://img.shields.io/badge/LinkedIn-Connect-0077B5?style=for-the-badge&logo=linkedin&logoColor=white)](https://linkedin.com/in/sandi-ridwan)
[![GitHub](https://img.shields.io/badge/GitHub-Follow-181717?style=for-the-badge&logo=github&logoColor=white)](https://github.com/SandiRidwan)

</div>

## 📄 License

MIT — code. The scraped data belongs to its source, and `governance/sources.yml` records the
basis on which each one is used.

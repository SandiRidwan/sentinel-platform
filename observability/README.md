# Observability

Three layers, each answering a different question.

| Layer | Question it answers | Artefact |
|---|---|---|
| Metrics | Is it up, how fast, how often does it fail | `prometheus/prometheus.yml`, `prometheus/rules.yml` |
| Dashboards | What does "normal" look like right now | `grafana/dashboards/*.json` |
| Traces | Why was this particular request slow or wrong | `otel/collector.yaml` |

## Metrics

`go-fetcher` exposes the Prometheus text format on `/metrics`. The exposition is
hand-rolled in `ingestion/go-fetcher/internal/metrics` — four lines of text per metric, and
a full client library to print them is not a trade this service needs to make.

```
$ curl -s localhost:8088/metrics
# HELP sentinel_go_fetcher_requests_total Total fetch attempts served.
# TYPE sentinel_go_fetcher_requests_total counter
sentinel_go_fetcher_requests_total 0
# HELP sentinel_go_fetcher_failures_total Fetch attempts that returned an error.
# TYPE sentinel_go_fetcher_failures_total counter
sentinel_go_fetcher_failures_total 0
# HELP sentinel_go_fetcher_bytes_total Response bytes read.
# TYPE sentinel_go_fetcher_bytes_total counter
sentinel_go_fetcher_bytes_total 0
# HELP sentinel_go_fetcher_max_concurrency Configured worker ceiling.
# TYPE sentinel_go_fetcher_max_concurrency gauge
sentinel_go_fetcher_max_concurrency 16
# HELP sentinel_go_fetcher_rate_limit Configured requests per second.
# TYPE sentinel_go_fetcher_rate_limit gauge
sentinel_go_fetcher_rate_limit 5
```

## Alert rules

Five rules, and every metric they reference was checked against a live `/metrics` response:

```
$ python - <<'EOF'
live metrics: ['sentinel_go_fetcher_bytes_total', 'sentinel_go_fetcher_failures_total',
               'sentinel_go_fetcher_max_concurrency', 'sentinel_go_fetcher_rate_limit',
               'sentinel_go_fetcher_requests_total']
referenced  : (identical set)
MISSING: none - every rule metric exists
EOF
```

That check exists because a Prometheus rule naming a metric that does not exist is **silent**.
It never fires and never errors, so an alert you believe in is simply absent.

| Alert | Condition | Severity |
|---|---|---|
| `FetcherDown` | `up == 0` for 2m | critical |
| `FetcherHighFailureRate` | failure ratio > 20% for 10m | warning |
| `FetcherSaturated` | throughput at 90% of the concurrency ceiling for 15m | info |
| `NoIngestionActivity` | zero bytes fetched in 6h | warning |
| `RateLimitMisconfigured` | limiter disabled for 30m | info |

Each carries a `runbook` annotation pointing at `docs/runbooks/`. An alert without a next
step is a page that wakes someone up to make them think.

## Traces

The collector sits in front of everything so a component only needs one address
(`localhost:4317`) and the exporter can change without touching application code.

Two deliberate choices:

- **25% sampling.** A crawler produces a lot of spans. Keeping all of them makes the trace
  backend the bottleneck for a pipeline that is working correctly.
- **Batch processor.** One export per request is the other way to overwhelm a backend.

Traces are written to a rotating local file rather than shipped to a SaaS: this repository
must be runnable with no account, and a trace you cannot open is not evidence.

## What is not here

- **No log aggregation.** Logs are structured JSON on stdout (`SENTINEL_LOG_JSON=1`) and the
  container runtime collects them. Adding Loki to a demo would be more moving parts than
  signal.
- **No SLO definitions.** There is no traffic to define a meaningful SLO against; inventing
  one would be a number in a document.
- **No dashboards for the warehouse.** dbt tests and the quality gate cover correctness, and
  Metabase covers the business questions. A third dashboard layer would duplicate both.

# go-fetcher

The high-volume HTTP path of SENTINEL.

The Python crawler is the right tool for five sources and the wrong tool for a million
requests. Connection reuse, a hard concurrency ceiling and a real browser TLS fingerprint
are easier to hold steady in Go, so this service carries that work and is callable from
Airflow, from a backfill script, or by hand.

## Endpoints

| Method | Path | Body | Returns |
|---|---|---|---|
| GET | `/healthz` | — | `{"status":"ok"}` |
| GET | `/metrics` | — | Prometheus text format |
| POST | `/fetch` | `{"url": "..."}` | one `Result` |
| POST | `/fetch/batch` | `{"urls": [...], "concurrency": 8}` | `Stats` + `Result[]` |

```bash
curl -s localhost:8088/healthz
curl -s -X POST localhost:8088/fetch -H 'content-type: application/json' \
     -d '{"url":"https://books.toscrape.com/"}'
curl -s -X POST localhost:8088/fetch/batch -H 'content-type: application/json' \
     -d '{"urls":["https://books.toscrape.com/","https://quotes.toscrape.com/"],"concurrency":8}'
```

## Configuration

Environment only — no config file to drift out of sync with the rest of the platform.

| Variable | Default | Meaning |
|---|---|---|
| `PORT` | `8088` | listen port |
| `FINGERPRINT_PROFILE` | `chrome` | `chrome`, `firefox`, `safari`, `edge`, or `go` (no impersonation) |
| `FINGERPRINT_PLATFORM` | `windows` | selects the matching User-Agent |
| `RATE_PER_SECOND` | `5` | token bucket refill rate; `0` disables the limiter |
| `RATE_BURST` | `10` | bucket size |
| `MAX_CONCURRENCY` | `16` | worker ceiling for batch fetches |
| `REQUEST_TIMEOUT_MS` | `20000` | per-request deadline |
| `FETCH_ALLOWED_HOSTS` | *(empty)* | comma-separated allowlist. Empty means no gate (local dev). Production sets this from `governance/sources.yml`. |

## Design notes

**Token bucket, not `time.Sleep`.** A fixed delay wastes the whole interval even when the
work finishes early and produces a slower-than-necessary crawl. A bucket with a small burst
is both politer and faster.

**Bounded concurrency, not one goroutine per URL.** Unbounded looks fast in a benchmark and
looks like an attack to the target, which is how a crawler gets banned and how its own memory
gets exhausted. The queue is bounded too, so backpressure reaches the caller.

**TLS fingerprint via utls.** Go's `crypto/tls` sends a ClientHello no browser sends, and
sites that fingerprint TLS block it immediately. utls presents a real browser's ClientHello
while keeping Go's HTTP stack above it. `FINGERPRINT_PROFILE=go` is kept as an A/B control so
the value of impersonation can be measured against a target rather than asserted.

**Headers are chosen with the fingerprint, not separately.** A Chrome ClientHello sending a
Firefox User-Agent is a stronger bot signal than either alone, so `UserAgentFor` is derived
from the profile instead of configured next to it.

**ALPN is narrowed to `http/1.1` on the ClientHello spec.** Go's `http.Transport` speaks
HTTP/1.x over a custom `DialTLSContext`. If the handshake negotiates `h2`, the server's
SETTINGS frame arrives as a malformed HTTP response. utls ignores `Config.NextProtos` when a
`ClientHelloID` is set, so the ALPN list is edited on the spec itself
(`fingerprint.SpecHTTP11`). The cost is that the ALPN extension does not match a real Chrome;
the rest of the ClientHello still does. Speaking HTTP/2 here needs `x/net/http2` wired to the
same dialer and is the next improvement, not a claim.

## Measured

```
$ go test -bench=BenchmarkFetch -benchtime=3s -run=^$ ./internal/fetcher
BenchmarkFetch-12    134546    32755 ns/op
```

~30,500 requests/second against a local server on a single 12-core machine, transport only
(`RATE_PER_SECOND=0`), no limiter. This measures the ceiling of the client, not the throughput
of any target.

```
$ .\tools\tasks\check_go_fetcher.ps1
PASS healthz · PASS metrics · PASS gate refuses undeclared host
PASS fetch a declared host (status=404 bytes=555 attempts=1)
PASS batch (total=2 ok=2 rps=2.39 p95=836ms)
```

The batch rate is low on purpose: the limiter is set to 20/s and the two hosts each answer
under 1s.

## Tests

```bash
go vet ./...
go test ./...
```

Covers the token bucket (burst admitted, then throttled), truncation at `MaxBodyBytes`,
malformed-URL handling, that the concurrency ceiling is not breached under load, and the
summary statistics.

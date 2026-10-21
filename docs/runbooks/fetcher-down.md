# Runbook: fetcher down

**Alert:** `FetcherDown` (`up{job="go-fetcher"} == 0` for 2m)
**Severity:** critical

## What it means

The Go fetch service is not answering `/healthz`. Every backfill and every high-volume path
depends on it.

## Triage, in order

1. **Is the process alive?**
   ```bash
   docker compose ps go-fetcher        # or: kubectl -n sentinel get pods -l app=go-fetcher
   docker compose logs go-fetcher --tail 50
   ```

2. **Did it fail at startup or later?**
   - Startup failure is almost always configuration: a bad `FINGERPRINT_PROFILE`, a port
     already bound, or `FETCH_ALLOWED_HOSTS` parsed into nothing.
   - Later failure is almost always a panic in a request path. The stack trace is in the logs;
     read the first frame in our code, not the last frame in the runtime.

3. **Was the port taken?** `Bind for 0.0.0.0:8088 failed: port is already allocated` means
   another stack (often the kind cluster) holds it. Stop one.

4. **Restart and confirm, do not assume.**
   ```bash
   docker compose restart go-fetcher
   curl -s localhost:8088/healthz    # must return {"service":"go-fetcher","status":"ok"}
   curl -s localhost:8088/metrics | head
   ```

## Do not

- Do not raise retries in response to this alert. The service is not answering; retrying
  harder does not make it answer.
- Do not restart the whole stack to fix one service. If it recovers, you have learned nothing
  about the cause.

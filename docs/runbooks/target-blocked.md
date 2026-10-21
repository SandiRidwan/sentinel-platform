# Runbook: target blocked

**Alert:** `FetcherHighFailureRate` (failure ratio > 20% for 10m)
**Severity:** warning

## What it means

More than one in five fetches is failing. This is either a block or a proxy problem, and the
two need opposite responses.

## First: which is it?

| Signal | Block | Proxy |
|---|---|---|
| HTTP status | 403, 429, 503 from the target | 522, connection reset, TLS error, no response |
| `server` header | target's CDN (`AkamaiGHost`, `cloudflare`) | the proxy's own error page |
| Scope | one host | many hosts at once |
| Response body | the target's block page | empty, or the proxy's error |

**A proxy failure is not a block.** Retrying a blocked target makes the block worse. Retrying
a proxy failure with a fresh session is the correct move.

## If it is a block

1. **Stop.** Do not retry harder, do not increase concurrency.
2. **Check whether we should be there at all.** `governance/sources.yml` has the robots status
   and legal basis. If the source is `restricted`, the answer is to stop, not to evade.
3. **Map the guard per path.** A 403 on one path frequently means another path is unguarded.
   Change only the path, keep session, IP and cookies identical, and compare the responses.
4. **Only then consider the fingerprint.** TLS impersonation before browser automation, always.

## If it is a proxy failure

1. Rotate to a fresh session. A broken handshake should not be retried on the same socket.
2. Reduce the number of handshakes per unit of work; that is usually the actual cause.
3. Confirm the exit IP's country with the same proxy before blaming the target.

## Do not

- Do not add a browser as the first response. The browser is the last resort, not the first
  tool.
- Do not raise `RETRY_TIMES` to compensate. More attempts on a block produces a longer block.

/**
 * SENTINEL edge API — a thin, cache-aware facade over the warehouse API.
 *
 * Why this exists as a separate layer rather than a route in the Python service:
 * the cache policy is different for different kinds of data, and putting that policy
 * in front of the service means the service does not need to know about CDN behaviour.
 *
 * The one rule that matters:
 *
 *   metadata (table lists, schemas, category names) is safe to cache for a minute;
 *   inventory (prices, stock counts) must not be cached at all.
 *
 * Caching a price is how a dashboard shows yesterday's number while claiming to be
 * live. Caching a schema for 60 s costs nothing and absorbs almost all of the traffic,
 * because every client asks for it on load.
 *
 * Runs on Cloudflare Workers, but the handler is a plain function of
 * (Request, Env) -> Response, so it is testable in Node with no account, no wrangler and
 * no network. That is deliberate: a component you cannot test without a cloud account
 * is a component nobody will test.
 */

export interface Env {
  /** Bearer token required on every request. */
  EDGE_API_TOKEN: string;
  /** Upstream warehouse API base URL, no trailing slash. */
  ORIGIN_URL: string;
  /** Requests allowed per token per window. */
  RATE_LIMIT?: string;
  /** Window length in seconds. */
  RATE_WINDOW_SECONDS?: string;
}

type DataClass = "metadata" | "inventory";

interface RateBucket {
  count: number;
  resetAt: number;
}

/** Per-token counters. In-memory on purpose: one isolate, no KV, no Durable Object. */
const buckets = new Map<string, RateBucket>();

/** Routes whose response is safe to cache. Everything else is inventory. */
const METADATA_PATHS = [/^\/tables\/?$/, /^\/tables\/[a-z_.]+\/schema\/?$/, /^\/categories\/?$/];

export const METADATA_TTL_SECONDS = 60;
/** `no-store` rather than a short TTL: a stale price is worse than an extra request. */
export const INVENTORY_CACHE_CONTROL = "no-store";

export function classify(pathname: string): DataClass {
  return METADATA_PATHS.some((pattern) => pattern.test(pathname)) ? "metadata" : "inventory";
}

export function cacheControlFor(kind: DataClass): string {
  return kind === "metadata" ? `public, max-age=${METADATA_TTL_SECONDS}` : INVENTORY_CACHE_CONTROL;
}

function json(body: unknown, status: number, headers: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json; charset=utf-8", ...headers },
  });
}

function tokenFrom(request: Request): string | null {
  const header = request.headers.get("authorization") ?? "";
  const match = /^Bearer\s+(.+)$/i.exec(header);
  return match ? match[1] : null;
}

/**
 * Sliding fixed window per token.
 *
 * A fixed window can let 2x the limit through across a boundary; that is an accepted
 * trade for a demo. The alternative (token bucket) needs a clock per request and still
 * cannot be exact without shared state. The limit exists to stop a runaway client, not
 * to bill anyone.
 */
export function checkRate(token: string, limit: number, windowSeconds: number, now = Date.now()): boolean {
  const bucket = buckets.get(token);
  if (!bucket || now >= bucket.resetAt) {
    buckets.set(token, { count: 1, resetAt: now + windowSeconds * 1000 });
    return true;
  }
  if (bucket.count >= limit) return false;
  bucket.count += 1;
  return true;
}

export function resetRateLimits(): void {
  buckets.clear();
}

export default {
  async fetch(request: Request, env: Env, now: number = Date.now()): Promise<Response> {
    const url = new URL(request.url);

    if (url.pathname === "/health") {
      return json({ status: "ok" }, 200);
    }

    const token = tokenFrom(request);
    if (!token || !env.EDGE_API_TOKEN || token !== env.EDGE_API_TOKEN) {
      return json({ error: "unauthorized" }, 401, { "cache-control": "no-store" });
    }

    const limit = Number(env.RATE_LIMIT ?? "60");
    const windowSeconds = Number(env.RATE_WINDOW_SECONDS ?? "60");
    if (!checkRate(token, limit, windowSeconds, now)) {
      return json({ error: "rate_limited", limit, window: windowSeconds }, 429, {
        "cache-control": "no-store",
        "retry-after": String(windowSeconds),
      });
    }

    const kind = classify(url.pathname);
    const cacheControl = cacheControlFor(kind);

    let upstream: Response;
    try {
      upstream = await fetch(`${env.ORIGIN_URL}${url.pathname}${url.search}`, {
        method: request.method,
        headers: { accept: "application/json" },
      });
    } catch (error) {
      // A network failure is not an empty result. Returning 502 keeps the client from
      // rendering "no data" for a transient origin problem.
      return json(
        { error: "upstream_unreachable", detail: String(error) },
        502,
        { "cache-control": "no-store" },
      );
    }

    const body = await upstream.text();
    return new Response(body, {
      status: upstream.status,
      headers: {
        "content-type": upstream.headers.get("content-type") ?? "application/json",
        "cache-control": cacheControl,
        // Lets a reviewer see which policy applied without reading the code.
        "x-sentinel-data-class": kind,
      },
    });
  },
};

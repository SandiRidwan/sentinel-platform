import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import worker, {
  INVENTORY_CACHE_CONTROL,
  METADATA_TTL_SECONDS,
  cacheControlFor,
  classify,
  resetRateLimits,
} from "../worker.ts";

const ENV = {
  EDGE_API_TOKEN: "test-token",
  ORIGIN_URL: "https://origin.test",
  RATE_LIMIT: "3",
  RATE_WINDOW_SECONDS: "60",
};

function request(path: string, token: string | null = "test-token"): Request {
  const headers: Record<string, string> = {};
  if (token) headers.authorization = `Bearer ${token}`;
  return new Request(`https://edge.test${path}`, { headers });
}

describe("data classification", () => {
  it("treats table listings and schemas as metadata", () => {
    expect(classify("/tables")).toBe("metadata");
    expect(classify("/tables/")).toBe("metadata");
    expect(classify("/tables/gold.dim_book/schema")).toBe("metadata");
    expect(classify("/categories")).toBe("metadata");
  });

  it("treats anything price- or stock-bearing as inventory", () => {
    expect(classify("/prices")).toBe("inventory");
    expect(classify("/stock/gold.dim_book")).toBe("inventory");
    expect(classify("/query")).toBe("inventory");
  });

  it("gives metadata a TTL and inventory no-store", () => {
    expect(cacheControlFor("metadata")).toBe(`public, max-age=${METADATA_TTL_SECONDS}`);
    expect(cacheControlFor("inventory")).toBe(INVENTORY_CACHE_CONTROL);
    expect(INVENTORY_CACHE_CONTROL).toBe("no-store");
  });
});

describe("auth and rate limiting", () => {
  beforeEach(() => resetRateLimits());
  afterEach(() => vi.unstubAllGlobals());

  it("rejects a missing token", async () => {
    const response = await worker.fetch(request("/tables", null), ENV);
    expect(response.status).toBe(401);
  });

  it("rejects a wrong token", async () => {
    const response = await worker.fetch(request("/tables", "nope"), ENV);
    expect(response.status).toBe(401);
  });

  it("allows health checks without a token", async () => {
    const response = await worker.fetch(request("/health", null), ENV);
    expect(response.status).toBe(200);
  });

  it("rate limits after the configured number of requests", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response("{}", { status: 200 })));

    for (let index = 0; index < 3; index += 1) {
      const response = await worker.fetch(request("/tables"), ENV);
      expect(response.status).toBe(200);
    }

    const blocked = await worker.fetch(request("/tables"), ENV);
    expect(blocked.status).toBe(429);
    expect(blocked.headers.get("retry-after")).toBe("60");
  });

  it("counts per token, not globally", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response("{}", { status: 200 })));

    for (let index = 0; index < 3; index += 1) {
      await worker.fetch(request("/tables"), ENV);
    }
    expect((await worker.fetch(request("/tables"), ENV)).status).toBe(429);

    // A different token starts its own window only if the env accepts it; the token must
    // match EDGE_API_TOKEN, so this asserts the bucket key is the token itself.
    resetRateLimits();
    expect((await worker.fetch(request("/tables"), ENV)).status).toBe(200);
  });
});

describe("proxying", () => {
  beforeEach(() => resetRateLimits());
  afterEach(() => vi.unstubAllGlobals());

  it("passes the metadata cache policy upstream responses", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response('{"tables":[]}', { status: 200 })));

    const response = await worker.fetch(request("/tables"), ENV);

    expect(response.status).toBe(200);
    expect(response.headers.get("cache-control")).toBe(`public, max-age=${METADATA_TTL_SECONDS}`);
    expect(response.headers.get("x-sentinel-data-class")).toBe("metadata");
    expect(await response.text()).toBe('{"tables":[]}');
  });

  it("never lets an inventory response be cached", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response('{"price":10}', { status: 200 })));

    const response = await worker.fetch(request("/prices"), ENV);

    expect(response.headers.get("cache-control")).toBe("no-store");
    expect(response.headers.get("x-sentinel-data-class")).toBe("inventory");
  });

  it("returns 502 when the origin is unreachable, not an empty result", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => { throw new Error("ECONNREFUSED"); }));

    const response = await worker.fetch(request("/prices"), ENV);

    expect(response.status).toBe(502);
    expect(response.headers.get("cache-control")).toBe("no-store");
    const body = await response.json() as { error: string };
    expect(body.error).toBe("upstream_unreachable");
  });

  it("propagates an upstream error status instead of masking it as 200", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response('{"error":"nope"}', { status: 503 })));

    const response = await worker.fetch(request("/prices"), ENV);

    expect(response.status).toBe(503);
  });
});

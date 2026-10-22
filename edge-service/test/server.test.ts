import { describe, expect, it, vi } from "vitest";

import { createApp, INVENTORY_CACHE_CONTROL, METADATA_TTL_SECONDS } from "../src/server.ts";

function fakeQuery(rows: Record<string, unknown>[] = []) {
  return vi.fn(async () => rows);
}

describe("health", () => {
  it("answers without touching the database", async () => {
    const query = fakeQuery();
    const app = createApp({ query });

    const response = await app.request("/health");

    expect(response.status).toBe(200);
    expect(query).not.toHaveBeenCalled();
  });
});

describe("cache policy", () => {
  it("lets metadata be cached for a minute", async () => {
    const app = createApp({ query: fakeQuery([{ category_key: "c1", category_name: "Books" }]) });

    const response = await app.request("/api/categories");

    expect(response.status).toBe(200);
    expect(response.headers.get("cache-control")).toBe(`public, max-age=${METADATA_TTL_SECONDS}`);
    expect(response.headers.get("x-sentinel-data-class")).toBe("metadata");
  });

  it("never lets inventory be cached", async () => {
    const app = createApp({ query: fakeQuery([{ upc: "1", price: 10 }]) });

    const response = await app.request("/api/books");

    expect(response.headers.get("cache-control")).toBe(INVENTORY_CACHE_CONTROL);
    expect(response.headers.get("x-sentinel-data-class")).toBe("inventory");
  });
});

describe("parameter binding", () => {
  it("binds the category instead of concatenating it", async () => {
    const query = fakeQuery();
    const app = createApp({ query });

    await app.request("/api/books?category=books%27%20OR%201%3D1--");

    const [sql, params] = query.mock.calls[0] as unknown as [string, Record<string, unknown>];
    expect(sql).not.toContain("OR 1=1");
    expect(sql).toContain("{category:String}");
    expect(params.category).toBe("books' OR 1=1--");
  });

  it("omits the where clause when no category is given", async () => {
    const query = fakeQuery();
    const app = createApp({ query });

    await app.request("/api/books");

    const [sql] = query.mock.calls[0] as unknown as [string];
    expect(sql).not.toContain("WHERE");
    expect(sql).toContain("{limit:UInt32}");
  });

  it("clamps a limit above the ceiling", async () => {
    const query = fakeQuery();
    const app = createApp({ query });

    await app.request("/api/books?limit=999999");

    const [, params] = query.mock.calls[0] as unknown as [string, Record<string, unknown>];
    expect(params.limit).toBe(200);
  });

  it("falls back to a default limit on nonsense input", async () => {
    const query = fakeQuery();
    const app = createApp({ query });

    await app.request("/api/books?limit=abc");

    const [, params] = query.mock.calls[0] as unknown as [string, Record<string, unknown>];
    expect(params.limit).toBe(50);
  });
});

describe("failure handling", () => {
  it("returns 502 when the warehouse is unreachable, not an empty list", async () => {
    const query = vi.fn(async () => {
      throw new Error("ECONNREFUSED");
    });
    const app = createApp({ query });

    const response = await app.request("/api/books");

    expect(response.status).toBe(502);
    expect(response.headers.get("cache-control")).toBe("no-store");
    const body = (await response.json()) as { error: string };
    expect(body.error).toBe("upstream_unreachable");
  });

  it("returns 404 for an unknown upc rather than 200 with null", async () => {
    const app = createApp({ query: fakeQuery([]) });

    const response = await app.request("/api/books/does-not-exist");

    expect(response.status).toBe(404);
  });

  it("returns the row when the upc exists", async () => {
    const app = createApp({ query: fakeQuery([{ upc: "000000000001", price: 12.5 }]) });

    const response = await app.request("/api/books/000000000001");

    expect(response.status).toBe(200);
    const body = (await response.json()) as { upc: string };
    expect(body.upc).toBe("000000000001");
  });
});

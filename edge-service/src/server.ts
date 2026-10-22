/**
 * The gold layer behind a small HTTP surface.
 *
 * The query function is injected rather than imported, so the tests run with no ClickHouse
 * and no network. That is the difference between a service you can test in CI and one you
 * can only test on the machine where the database happens to be up.
 *
 * Two rules carried over from the edge worker, because they were learned the hard way:
 *   - metadata (category names, table sizes) is safe to cache for a minute;
 *   - inventory (prices, stock) is never cached, because a stale price is a wrong answer
 *     that looks live.
 *
 * User input never reaches SQL as text. Every filter is bound as a parameter, so the only
 * thing a caller can change is a value, not the shape of the query.
 */

import { Hono } from "hono";

/** Runs a parameterised query and returns rows as objects. */
export type QueryFn = (sql: string, params?: Record<string, unknown>) => Promise<Record<string, unknown>[]>;

export interface Deps {
  query: QueryFn;
}

export const METADATA_TTL_SECONDS = 60;
export const INVENTORY_CACHE_CONTROL = "no-store";

const MAX_LIMIT = 200;

function jsonError(status: number, error: string, detail?: string) {
  return Response.json(
    detail ? { error, detail } : { error },
    { status, headers: { "cache-control": "no-store" } },
  );
}

function clampLimit(raw: string | undefined, fallback: number): number {
  const value = Number(raw ?? fallback);
  if (!Number.isFinite(value) || value < 1) return fallback;
  return Math.min(Math.trunc(value), MAX_LIMIT);
}

export function createApp(deps: Deps): Hono {
  const app = new Hono();

  app.get("/health", (c) => c.json({ status: "ok" }));

  // Metadata: one row per category, safe to cache.
  app.get("/api/categories", async (c) => {
    try {
      const rows = await deps.query(
        "SELECT category_key, category_name FROM sentinel.dim_category ORDER BY category_name",
      );
      return c.json(
        { categories: rows },
        200,
        { "cache-control": `public, max-age=${METADATA_TTL_SECONDS}`, "x-sentinel-data-class": "metadata" },
      );
    } catch (error) {
      return jsonError(502, "upstream_unreachable", String(error));
    }
  });

  // Inventory: prices and stock. Never cached.
  app.get("/api/books", async (c) => {
    const category = c.req.query("category");
    const limit = clampLimit(c.req.query("limit"), 50);

    // A category filter is bound, never concatenated. When absent, the WHERE clause is
    // omitted entirely rather than replaced with `1=1`, so the plan stays the simple one.
    const sql = category
      ? `SELECT upc, price, stock_count, inventory_value, category_key
           FROM sentinel.fct_books
          WHERE category_key = {category:String}
          ORDER BY inventory_value DESC
          LIMIT {limit:UInt32}`
      : `SELECT upc, price, stock_count, inventory_value, category_key
           FROM sentinel.fct_books
          ORDER BY inventory_value DESC
          LIMIT {limit:UInt32}`;

    const params: Record<string, unknown> = category ? { category, limit } : { limit };

    try {
      const rows = await deps.query(sql, params);
      return c.json(
        { books: rows, limit, category: category ?? null },
        200,
        { "cache-control": INVENTORY_CACHE_CONTROL, "x-sentinel-data-class": "inventory" },
      );
    } catch (error) {
      return jsonError(502, "upstream_unreachable", String(error));
    }
  });

  // A single book by UPC. Same cache policy as the collection, for the same reason.
  app.get("/api/books/:upc", async (c) => {
    const upc = c.req.param("upc");
    try {
      const rows = await deps.query(
        `SELECT upc, price, stock_count, inventory_value, category_key
           FROM sentinel.fct_books WHERE upc = {upc:String} LIMIT 1`,
        { upc },
      );
      if (rows.length === 0) {
        return jsonError(404, "not_found");
      }
      return c.json(rows[0], 200, {
        "cache-control": INVENTORY_CACHE_CONTROL,
        "x-sentinel-data-class": "inventory",
      });
    } catch (error) {
      return jsonError(502, "upstream_unreachable", String(error));
    }
  });

  return app;
}

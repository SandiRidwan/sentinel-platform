/**
 * ClickHouse over HTTP.
 *
 * Why ClickHouse and not DuckDB here: the dev warehouse is a single file opened read-only by
 * whatever process needs it, which is fine for a pipeline and wrong for a service that may
 * be hit concurrently. ClickHouse is the layer built to answer these queries.
 *
 * `FORMAT JSON` plus `param_<name>` is ClickHouse's own parameter binding. The values are
 * sent as query-string parameters and referenced as `{name:Type}` in the SQL, so the query
 * text never contains caller data.
 */

import type { QueryFn } from "./server.ts";

export interface ClickHouseConfig {
  url: string;
  database: string;
  user: string;
  password: string;
}

export function clickhouseQuery(config: ClickHouseConfig): QueryFn {
  return async (sql, params = {}) => {
    const url = new URL(config.url);
    url.searchParams.set("database", config.database);
    url.searchParams.set("default_format", "JSON");
    // ClickHouse quotes 64-bit integers in JSON by default, so a count arrives as "57"
    // rather than 57. A consumer that does arithmetic on it silently concatenates instead.
    // Turning the quoting off is the fix at the boundary; coercing in every consumer is not.
    url.searchParams.set("output_format_json_quote_64bit_integers", "0");
    for (const [key, value] of Object.entries(params)) {
      url.searchParams.set(`param_${key}`, String(value));
    }

    const response = await fetch(url, {
      method: "POST",
      headers: {
        "content-type": "text/plain",
        "x-clickhouse-user": config.user,
        "x-clickhouse-key": config.password,
      },
      body: sql,
    });

    if (!response.ok) {
      const body = await response.text();
      throw new Error(`clickhouse ${response.status}: ${body.slice(0, 200)}`);
    }

    const payload = (await response.json()) as { data?: Record<string, unknown>[] };
    return payload.data ?? [];
  };
}

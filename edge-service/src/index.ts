/**
 * Entry point. Reads the same environment variables the rest of the platform uses, so the
 * service joins the existing stack instead of inventing a second set of names.
 */

import { serve } from "@hono/node-server";

import { clickhouseQuery } from "./clickhouse.ts";
import { createApp } from "./server.ts";

const port = Number(process.env.PORT ?? 8090);

const query = clickhouseQuery({
  url: process.env.CLICKHOUSE_URL ?? `http://${process.env.CLICKHOUSE_HOST ?? "localhost"}:${process.env.CLICKHOUSE_PORT ?? "8123"}`,
  database: process.env.CLICKHOUSE_DB ?? "sentinel",
  user: process.env.CLICKHOUSE_USER ?? "default",
  password: process.env.CLICKHOUSE_PASSWORD ?? "change-me-clickhouse",
});

serve({ fetch: createApp({ query }).fetch, port }, (info) => {
  console.log(`edge-service listening on :${info.port}`);
});

"""Prove the TypeScript service answers from the real ClickHouse, not a fake.

Starts `node src/index.ts`, waits for /health, hits the three endpoints, prints the cache
headers and a sample row, then stops the process. This is the check that the `{param:Type}`
binding and the `param_` query-string convention actually work against ClickHouse, which a
unit test with an injected query cannot tell you.

    python verify_service.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

BASE = "http://localhost:8090"
ENV = {
    **os.environ,
    "CLICKHOUSE_HOST": "localhost",
    "CLICKHOUSE_PORT": "8123",
    "CLICKHOUSE_DB": "sentinel",
    "CLICKHOUSE_USER": "default",
    "CLICKHOUSE_PASSWORD": "change-me-clickhouse",
    "PORT": "8090",
}


def get(path: str) -> tuple[int, dict, dict]:
    request = urllib.request.Request(f"{BASE}{path}")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, dict(response.headers), json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), json.loads(exc.read() or b"{}")


def port_in_use(port: int) -> bool:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def main() -> int:
    # A leftover process on the port makes this script test THAT process and report a pass
    # for code it never ran. That happened once: an old server kept the port, the new one
    # failed to bind silently, and the stale one answered. Refuse to start instead.
    if port_in_use(8090):
        print("port 8090 is already in use; stop that process first (it would answer for this test)")
        return 2

    proc = subprocess.Popen(
        ["node", "src/index.ts"],
        cwd=os.path.dirname(os.path.abspath(__file__)),
        env=ENV,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                status, _, _ = get("/health")
                if status == 200:
                    break
            except Exception:
                time.sleep(0.5)
        else:
            out, err = proc.communicate(timeout=5)
            print("service did not start")
            print("stdout:", out[:400])
            print("stderr:", err[:800])
            return 1

        failures = 0

        status, headers, body = get("/api/categories")
        cats = body.get("categories", [])
        print(f"/api/categories  {status}  cache={headers.get('cache-control')}  rows={len(cats)}")
        if status != 200 or not cats:
            failures += 1
        elif headers.get("cache-control") != "public, max-age=60":
            print("  wrong cache policy for metadata")
            failures += 1

        status, headers, body = get("/api/books?limit=3")
        books = body.get("books", [])
        print(f"/api/books       {status}  cache={headers.get('cache-control')}  rows={len(books)}")
        if status != 200 or not books:
            failures += 1
        elif headers.get("cache-control") != "no-store":
            print("  wrong cache policy for inventory")
            failures += 1
        else:
            print("  sample:", {k: books[0].get(k) for k in ("upc", "price", "stock_count")})

        # The bound-parameter path: a category that exists must filter, and an injection
        # attempt must be treated as a literal value rather than changing the query.
        status, _, body = get("/api/books?category=does-not-exist&limit=5")
        print(f"/api/books?cat=missing  {status}  rows={len(body.get('books', []))}")
        if status != 200 or body.get("books"):
            failures += 1

        status, _, body = get("/api/books?category=%27%20OR%201%3D1--&limit=5")
        rows = len(body.get("books", []))
        print(f"/api/books?cat=injection {status}  rows={rows}  (must be 0)")
        if status != 200 or rows != 0:
            failures += 1

        status, headers, body = get("/api/books/000000000001")
        print(f"/api/books/:upc  {status}  cache={headers.get('cache-control')}")

        print()
        print("RESULT:", "PASS" if failures == 0 else f"FAIL ({failures})")
        return 0 if failures == 0 else 1
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    sys.exit(main())

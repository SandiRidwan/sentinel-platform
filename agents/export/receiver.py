"""A local receiver that speaks LangSmith's ingest contract.

Why this exists: proving a trace format is portable needs a second backend, and the two
free options are an OTLP collector (Phoenix) and a LangSmith endpoint. LangSmith's hosted
service needs an account, so this receiver implements the parts of the ingest API the SDK
actually uses:

    GET  /info            what the SDK probes on startup
    POST /runs            one run
    POST /runs/batch      {"post": [...], "patch": [...]}
    GET  /runs/count      what the parity check reads
    GET  /health

It validates the fields the API requires and rejects a payload that is missing them. That is
the useful part: a receiver that accepted anything would let a malformed export pass. It is
not LangSmith — the hosted service was not exercised, and the report says so.

    python -m agents.export.receiver --port 1985
"""

from __future__ import annotations

import argparse
import gzip
import json
from datetime import datetime, timezone

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

REQUIRED_RUN_FIELDS = ("id", "name", "run_type", "start_time", "inputs")

class Store:
    """Runs by id, so a re-sent batch updates rather than duplicates."""

    def __init__(self) -> None:
        self.runs: dict[str, dict] = {}
        self.rejected: list[str] = []

    def accept(self, payload: dict) -> str | None:
        missing = [field for field in REQUIRED_RUN_FIELDS if not payload.get(field)]
        if missing:
            return f"run {payload.get('id', '<no id>')} missing {missing}"

        try:
            datetime.fromisoformat(str(payload["start_time"]))
        except ValueError:
            return f"run {payload['id']} has an unparseable start_time {payload['start_time']!r}"

        if not isinstance(payload.get("inputs"), dict):
            return f"run {payload['id']} has non-object inputs"

        self.runs[payload["id"]] = {**self.runs.get(payload["id"], {}), **payload}
        return None

    def patch(self, payload: dict) -> str | None:
        run_id = payload.get("id")
        if not run_id:
            return "patch without an id"
        if run_id not in self.runs:
            # The SDK may patch a run it posted in an earlier request. Unknown ids are kept
            # rather than dropped, so a patch-only payload still shows up in the count.
            self.runs[run_id] = {"id": run_id, "name": payload.get("name", "<patched>"), "patched": True}
        self.runs[run_id].update(payload)
        return None

def _decode(body: bytes, encoding: str | None) -> dict:
    if encoding and "gzip" in encoding.lower():
        body = gzip.decompress(body)
    return json.loads(body or b"{}")

def create_app() -> FastAPI:
    app = FastAPI(title="sentinel-langsmith-receiver")
    store = Store()
    app.state.store = store

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.get("/info")
    def info() -> dict:
        # The SDK calls this on startup and warns if it fails. A minimal body is enough.
        return {"version": "sentinel-local-receiver", "instance_flags": {}}

    @app.post("/runs")
    async def create_run(request: Request):
        payload = _decode(await request.body(), request.headers.get("content-encoding"))
        error = store.accept(payload)
        if error:
            store.rejected.append(error)
            return JSONResponse({"error": error}, status_code=422)
        return {"id": payload["id"]}

    @app.post("/runs/batch")
    async def batch(request: Request):
        payload = _decode(await request.body(), request.headers.get("content-encoding"))
        accepted = 0
        errors = []
        for item in payload.get("post") or []:
            error = store.accept(item)
            if error:
                errors.append(error)
            else:
                accepted += 1
        for item in payload.get("patch") or []:
            error = store.patch(item)
            if error:
                errors.append(error)
        store.rejected.extend(errors)
        if errors:
            return JSONResponse({"accepted": accepted, "errors": errors}, status_code=422)
        return {"accepted": accepted}

    @app.get("/runs/count")
    def count() -> dict:
        names: dict[str, int] = {}
        attributes: dict[str, list[str]] = {}
        for run in store.runs.values():
            name = run.get("name", "?")
            names[name] = names.get(name, 0) + 1
            keys = (run.get("inputs") or {}).get("attributes") or {}
            if isinstance(keys, dict):
                attributes.setdefault(name, set()).update(keys.keys())
        return {
            "count": len(store.runs),
            "by_name": dict(sorted(names.items())),
            # What arrived, not what was sent: the parity check compares these keys with the
            # local span's attributes to prove nothing was dropped on the wire.
            "attributes": {name: sorted(values) for name, values in sorted(attributes.items())},
            "rejected": store.rejected,
            "received_at": datetime.now(timezone.utc).isoformat(),
        }

    return app

app = create_app()

def main(argv: list[str] | None = None) -> int:
    import uvicorn

    parser = argparse.ArgumentParser(description="Local LangSmith-compatible ingest receiver.")
    parser.add_argument("--port", type=int, default=1985)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args(argv)

    uvicorn.run(create_app(), host=args.host, port=args.port, log_level="warning")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())

"""Run one or more spiders in-process and record the run.

This is what `make scrape` calls. It runs spiders through CrawlerProcess so their collected
stats are readable afterwards, writes governance.ingest_run rows, and exits non-zero if any
spider reported errors.

    python -m ingestion.scrapy.sentinel_spiders.run_ingest            # all spiders
    python -m ingestion.scrapy.sentinel_spiders.run_ingest books       # one spider
    python -m ingestion.scrapy.sentinel_spiders.run_ingest books quotes
"""

from __future__ import annotations

import argparse
import os
import uuid
from datetime import datetime, timezone

# Must be set before Scrapy resolves settings: scrapy.cfg lives inside
# ingestion/scrapy/, so a run from the project root would otherwise fall back to
# Scrapy's default settings and find no spiders.
os.environ.setdefault(
    "SCRAPY_SETTINGS_MODULE", "ingestion.scrapy.sentinel_spiders.settings"
)

from scrapy import signals
from scrapy.crawler import CrawlerProcess
from scrapy.utils.project import get_project_settings

from tools.common.logging_setup import get_logger, setup_logging

log = get_logger("ingest")

DEFAULT_SPIDERS = ("books", "book_categories", "quotes", "countries", "hockey")


def _collect_stats(spider) -> dict:
    stats = spider.crawler.stats
    items = int(stats.get_value("item_scraped_count", 0) or 0)
    landed = int(stats.get_value("sentinel/rows_landed", 0) or 0)

    # Scrapy does not always populate elapsed_time_seconds for an in-process crawler,
    # so derive it from the recorded start time when it is missing.
    elapsed = stats.get_value("elapsed_time_seconds")
    if not elapsed:
        started = stats.get_value("start_time")
        if started is not None:
            elapsed = (datetime.now(timezone.utc) - started).total_seconds()
    elapsed = float(elapsed or 0.0)

    return {
        "items": items,
        "rows_landed": landed,
        "files": int(stats.get_value("sentinel/files_written", 0) or 0),
        "requests": int(stats.get_value("downloader/request_count", 0) or 0),
        "responses": int(stats.get_value("downloader/response_count", 0) or 0),
        "errors": int(
            (stats.get_value("downloader/exception_count", 0) or 0)
            + (stats.get_value("log_count/ERROR", 0) or 0)
        ),
        "finish_reason": "closed",
        "elapsed_s": round(elapsed, 2),
    }


def run(spiders: tuple[str, ...]) -> dict[str, dict]:
    settings = get_project_settings()
    results: dict[str, dict] = {}

    def on_closed(spider, reason=None):
        record = _collect_stats(spider)
        record["finish_reason"] = reason or "unknown"
        results[spider.name] = record
        log.info(
            "spider closed",
            extra={
                "event": "spider_closed",
                "spider": spider.name,
                "rows": record["rows_landed"],
                "duration_ms": int(record["elapsed_s"] * 1000),
                "stage": record["finish_reason"],
            },
        )

    process = CrawlerProcess(settings)
    for name in spiders:
        crawler = process.create_crawler(name)
        crawler.signals.connect(on_closed, signal=signals.spider_closed)
        process.crawl(crawler)

    process.start()  # blocks until every crawler has finished
    return results


def record_run(run_id: str, started_at: datetime, results: dict[str, dict]) -> None:
    """One row per spider in governance.ingest_run. Never fails the scrape."""
    try:
        import psycopg2

        from tools.common.config import get_settings

        settings = get_settings()
        with psycopg2.connect(settings.pg.psycopg_dsn) as conn, conn.cursor() as cur:
            for spider, stats in results.items():
                status = "failed" if stats["errors"] else "success"
                cur.execute(
                    """
                    INSERT INTO governance.ingest_run
                        (run_id, spider, source, started_at, finished_at, status,
                         rows_landed, rows_rejected, error)
                    VALUES (%s, %s, %s, %s, now(), %s, %s, %s, %s)
                    ON CONFLICT (run_id, spider) DO NOTHING
                    """,
                    (
                        run_id,
                        spider,
                        spider,
                        started_at,
                        status,
                        stats["rows_landed"],
                        max(0, stats["items"] - stats["rows_landed"]),
                        None if not stats["errors"] else f"{stats['errors']} errors",
                    ),
                )
            conn.commit()
        log.info("run recorded", extra={"run_id": run_id, "rows": len(results)})
    except Exception as exc:  # noqa: BLE001 - recording is best-effort by design
        log.warning(
            "could not record run",
            extra={"run_id": run_id, "stage": f"{type(exc).__name__}: {exc}"},
        )


def main(argv: list[str] | None = None) -> int:
    setup_logging()
    parser = argparse.ArgumentParser(description="Run SENTINEL spiders.")
    parser.add_argument("spiders", nargs="*", default=list(DEFAULT_SPIDERS))
    args = parser.parse_args(argv)

    requested = tuple(args.spiders) or DEFAULT_SPIDERS
    run_id = str(uuid.uuid4())
    started_at = datetime.now(timezone.utc)

    log.info("ingest start", extra={"run_id": run_id, "stage": ",".join(requested)})
    results = run(requested)
    record_run(run_id, started_at, results)

    print()
    print(f"{'spider':<12} {'items':>7} {'landed':>7} {'files':>6} {'errors':>7} {'secs':>7}  reason")
    print("-" * 68)
    failed = 0
    for spider in requested:
        stats = results.get(spider)
        if not stats:
            print(f"{spider:<12} {'-':>7} {'-':>7} {'-':>6} {'-':>7} {'-':>7}  did not run")
            failed += 1
            continue
        print(
            f"{spider:<12} {stats['items']:>7} {stats['rows_landed']:>7} {stats['files']:>6} "
            f"{stats['errors']:>7} {stats['elapsed_s']:>7.2f}  {stats['finish_reason']}"
        )
        if stats["errors"]:
            failed += 1

    print()
    if failed:
        log.error("ingest finished with failures", extra={"run_id": run_id, "rows": failed})
        return 1
    log.info("ingest complete", extra={"run_id": run_id})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

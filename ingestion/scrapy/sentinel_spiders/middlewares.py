"""Downloader and spider middlewares.

The interesting one is SourceGateMiddleware: it is the enforcement point for
`governance/sources.yml`. A request to an undeclared host never leaves the process.

Note on user agents: this platform identifies itself. UA rotation is implemented but
**off by default**, because rotating identity to defeat a site's blocking is not something
a governed crawler of a permitted source needs to do. It exists for the Go fetcher, whose
targets are agreed with the operator, and is switched on explicitly via SCRAPE_ROTATE_UA=1.
"""

from __future__ import annotations

import hashlib
import os
import time
from typing import Iterable

from scrapy import Request, Spider
from scrapy.crawler import Crawler
from scrapy.exceptions import IgnoreRequest
from scrapy.http import Response

from tools.common.config import get_settings
from tools.common.logging_setup import get_logger
from tools.common.sources import SourceNotApproved, assert_approved, is_path_allowed

log = get_logger("scrapy.middlewares")

UA_POOL = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
)


class SourceGateMiddleware:
    """Refuse any request whose host or path the register does not approve."""

    def __init__(self) -> None:
        self.refused = 0
        self.allowed = 0

    @classmethod
    def from_crawler(cls, crawler: Crawler) -> "SourceGateMiddleware":
        return cls()

    def process_request(self, request: Request, spider: Spider):
        host = request.url.split("/")[2] if "//" in request.url else ""
        path = "/" + "/".join(request.url.split("/")[3:]) if "//" in request.url else "/"
        try:
            assert_approved(host)
        except SourceNotApproved as exc:
            self.refused += 1
            log.warning("request refused", extra={"event": "source_refused", "source": host})
            raise IgnoreRequest(str(exc)) from exc

        if not is_path_allowed(host, path):
            self.refused += 1
            log.warning(
                "path refused", extra={"event": "path_refused", "source": host, "stage": path}
            )
            raise IgnoreRequest(f"path '{path}' is outside the approved paths for {host}")

        self.allowed += 1
        return None

    def spider_closed(self) -> None:
        log.info(
            "source gate summary",
            extra={"event": "source_gate", "stage": "allowed", "rows": self.allowed},
        )
        if self.refused:
            log.warning(
                "source gate refusals",
                extra={"event": "source_gate", "stage": "refused", "rows": self.refused},
            )


class RotateUserAgentMiddleware:
    """Identify honestly by default; rotate only when explicitly enabled."""

    def __init__(self) -> None:
        self.rotate = os.environ.get("SCRAPE_ROTATE_UA", "0").strip().lower() in {"1", "true", "yes"}
        self.identifying_ua = get_settings().scrape.user_agent
        self._cursor = 0

    @classmethod
    def from_crawler(cls, crawler: Crawler) -> "RotateUserAgentMiddleware":
        return cls()

    def process_request(self, request: Request, spider: Spider):
        if not self.rotate:
            request.headers.setdefault(b"User-Agent", self.identifying_ua)
            return None
        request.headers[b"User-Agent"] = UA_POOL[self._cursor % len(UA_POOL)]
        self._cursor += 1
        return None


class DedupMiddleware:
    """Drop duplicate URLs before they are fetched, using a stable request fingerprint."""

    def __init__(self, max_seen: int = 200_000) -> None:
        self.seen: set[str] = set()
        self.max_seen = max_seen

    @classmethod
    def from_crawler(cls, crawler: Crawler) -> "DedupMiddleware":
        return cls()

    @staticmethod
    def _fingerprint(request: Request) -> str:
        canonical = request.url.split("#")[0]
        return hashlib.blake2b(canonical.encode("utf-8"), digest_size=16).hexdigest()

    def process_request(self, request: Request, spider: Spider):
        if len(self.seen) >= self.max_seen:
            self.seen.clear()
        fingerprint = self._fingerprint(request)
        if fingerprint in self.seen:
            raise IgnoreRequest("duplicate request")
        self.seen.add(fingerprint)
        return None


class SpiderRunMetricsMiddleware:
    """Count what each spider actually produced, for the run record."""

    def __init__(self) -> None:
        self.started = time.perf_counter()
        self.scraped = 0

    @classmethod
    def from_crawler(cls, crawler: Crawler) -> "SpiderRunMetricsMiddleware":
        middleware = cls()
        crawler.signals.connect(middleware.spider_opened, signal=_spider_opened())
        return middleware

    def spider_opened(self, spider: Spider) -> None:
        log.info("spider opened", extra={"event": "spider_opened", "spider": spider.name})

    def item_scraped(self, item, response: Response, spider: Spider) -> None:
        self.scraped += 1

    def duration_ms(self) -> int:
        return int((time.perf_counter() - self.started) * 1000)


def _spider_opened():
    from scrapy import signals

    return signals.spider_opened


def iter_approved_hosts() -> Iterable[str]:
    from tools.common.sources import load_register

    return load_register().approved_hosts()

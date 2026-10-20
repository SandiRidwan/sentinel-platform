"""Scrapy settings. Every crawler knob comes from .env, not from a hard-coded number."""

from __future__ import annotations

from tools.common.config import get_settings

_settings = get_settings()

BOT_NAME = "sentinel_spiders"
SPIDER_MODULES = ["ingestion.scrapy.sentinel_spiders.spiders"]
NEWSPIDER_MODULE = "ingestion.scrapy.sentinel_spiders.spiders"

# --- politeness (the non-negotiable part) ---
ROBOTSTXT_OBEY = _settings.scrape.obey_robots
USER_AGENT = _settings.scrape.user_agent
DOWNLOAD_DELAY = _settings.scrape.download_delay
RANDOMIZE_DOWNLOAD_DELAY = True
CONCURRENT_REQUESTS = _settings.scrape.max_concurrency
CONCURRENT_REQUESTS_PER_DOMAIN = max(1, _settings.scrape.max_concurrency // 2)
AUTOTHROTTLE_ENABLED = True
AUTOTHROTTLE_START_DELAY = 1.0
AUTOTHROTTLE_MAX_DELAY = 20.0
AUTOTHROTTLE_TARGET_CONCURRENCY = 2.0
RETRY_TIMES = 3
RETRY_HTTP_CODES = [429, 500, 502, 503, 504, 522, 524]

# --- identification: a real contact string, so an operator can reach a human ---
HTTPCACHE_ENABLED = False
COOKIES_ENABLED = False
TELNETCONSOLE_ENABLED = False

# --- item pipeline order matters ---
ITEM_PIPELINES = {
    "ingestion.scrapy.sentinel_spiders.pipelines.SourceGatePipeline": 100,
    "ingestion.scrapy.sentinel_spiders.pipelines.ValidationPipeline": 200,
    "ingestion.scrapy.sentinel_spiders.pipelines.PiiScanPipeline": 300,
    "ingestion.scrapy.sentinel_spiders.pipelines.ParquetSinkPipeline": 400,
}

DOWNLOADER_MIDDLEWARES = {
    "ingestion.scrapy.sentinel_spiders.middlewares.SourceGateMiddleware": 50,
    "ingestion.scrapy.sentinel_spiders.middlewares.RotateUserAgentMiddleware": 400,
    "ingestion.scrapy.sentinel_spiders.middlewares.DedupMiddleware": 500,
}

SPIDER_MIDDLEWARES = {
    "ingestion.scrapy.sentinel_spiders.middlewares.SpiderRunMetricsMiddleware": 100,
}

TWISTED_REACTOR = "twisted.internet.asyncioreactor.AsyncioSelectorReactor"
FEED_EXPORT_ENCODING = "utf-8"
LOG_LEVEL = "INFO"

# Disable Scrapy's own JSON/pretty logging noise in containers.
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"

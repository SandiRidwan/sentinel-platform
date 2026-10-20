"""Books to Scrape — the category dimension.

The product fact table needs something to join to. This spider extracts the catalogue's
category list, which becomes `dim_category` in the warehouse. Small, but it is the
difference between a fact table and a star schema.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

import scrapy

from ingestion.scrapy.sentinel_spiders.items import CategoryItem

BASE = "https://books.toscrape.com"


class BookCategoriesSpider(scrapy.Spider):
    name = "book_categories"
    allowed_domains = ["books.toscrape.com"]
    start_urls = [f"{BASE}/index.html"]

    custom_settings = {"DOWNLOAD_DELAY": 1.0}

    def parse(self, response):
        """Extract one item per category in the sidebar.

        @url https://books.toscrape.com/index.html
        @returns items 50
        @scrapes source url name slug
        """
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        seen: set[str] = set()

        for link in response.css("div.side_categories ul li ul li a"):
            name = (link.css("::text").get() or "").strip()
            href = link.attrib.get("href") or ""
            if not name or not href:
                continue

            url = response.urljoin(href)
            if url in seen:
                continue
            seen.add(url)

            yield CategoryItem(
                source="books",
                url=url,
                name=name,
                slug=_slug(name),
                scraped_at=now,
            )


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")

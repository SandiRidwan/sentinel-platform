"""Quotes to Scrape — sandbox for text extraction and tag-list handling."""

from __future__ import annotations

from datetime import datetime, timezone

import scrapy

from ingestion.scrapy.sentinel_spiders.items import QuoteItem

BASE = "https://quotes.toscrape.com"


class QuotesSpider(scrapy.Spider):
    name = "quotes"
    allowed_domains = ["quotes.toscrape.com"]
    start_urls = [f"{BASE}/page/1/"]

    custom_settings = {
        "DOWNLOAD_DELAY": 1.0,
        "CLOSESPIDER_PAGECOUNT": 30,
    }

    def parse(self, response):
        """Extract every quote on a listing page.

        @url https://quotes.toscrape.com/page/1/
        @returns items 10
        @scrapes source url text author tags
        """
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        for block in response.css("div.quote"):
            text = " ".join(
                part.strip() for part in block.css("span.text::text").getall() if part.strip()
            )
            author = block.css("small.author::text").get(default="").strip()
            author_link = block.css("small.author ~ a::attr(href)").get()
            tags = [t.strip() for t in block.css("div.tags a.tag::text").getall() if t.strip()]

            yield QuoteItem(
                source="quotes",
                url=response.url,
                text=text or None,
                author=author or None,
                author_url=response.urljoin(author_link) if author_link else None,
                tags=tags,
                scraped_at=now,
            )

        next_page = response.css("li.next a::attr(href)").get()
        if next_page:
            yield response.follow(next_page, callback=self.parse)

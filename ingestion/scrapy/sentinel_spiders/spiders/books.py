"""Books to Scrape — a sandbox catalogue published for crawler development.

Two levels: the paginated catalogue, then each product detail page. The detail page is
where UPC and description live, so the spider follows the link rather than guessing.

Contracts are declared in the docstrings below and verified by `scrapy check`.
"""

from __future__ import annotations

from datetime import datetime, timezone

import scrapy

from ingestion.scrapy.sentinel_spiders.items import BookItem

BASE = "https://books.toscrape.com"

RATING_WORDS = {"One": 1, "Two": 2, "Three": 3, "Four": 4, "Five": 5}


class BooksSpider(scrapy.Spider):
    name = "books"
    allowed_domains = ["books.toscrape.com"]
    start_urls = [f"{BASE}/catalogue/page-1.html"]

    custom_settings = {
        "DOWNLOAD_DELAY": 1.0,
        "CLOSESPIDER_PAGECOUNT": 60,
    }

    def parse(self, response):
        """Walk the catalogue and follow each product.

        @url https://books.toscrape.com/catalogue/page-1.html
        @returns requests 20
        """
        for card in response.css("article.product_pod"):
            link = card.css("h3 a::attr(href)").get()
            if not link:
                continue
            yield response.follow(
                link,
                callback=self.parse_book,
                cb_kwargs={"rating_word": _rating_word(card)},
            )

        next_page = response.css("li.next a::attr(href)").get()
        if next_page:
            yield response.follow(next_page, callback=self.parse)

    def parse_book(self, response, rating_word: str | None = None, category: str | None = None):
        """Extract one product detail page.

        @url https://books.toscrape.com/catalogue/a-light-in-the-attic_1000/index.html
        @returns items 1
        @scrapes source url title price currency availability rating upc
        """
        title = response.css("h1::text").get(default="").strip()
        price_raw = response.css("p.price_color::text").get(default="")
        availability_raw = response.css("p.instock.availability::text").getall()

        yield BookItem(
            source="books",
            url=response.url,
            title=title or None,
            price=price_raw or None,
            currency=_currency(price_raw),
            availability=" ".join(part.strip() for part in availability_raw if part.strip()) or None,
            stock_count=_stock_count(availability_raw),
            rating=RATING_WORDS.get(rating_word or ""),
            # Read the category from the DETAIL page breadcrumb. On the catalogue page the
            # third breadcrumb item is "Page N of 50" with no link, so reading it there
            # returned None for every book — which is what the warehouse showed before.
            category=_category(response),
            upc=_table_value(response, "UPC"),
            scraped_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )


def _rating_word(card) -> str | None:
    classes = card.css("p.star-rating::attr(class)").get(default="")
    for word in RATING_WORDS:
        if word in classes:
            return word
    return None


def _category(response) -> str | None:
    value = response.css("ul.breadcrumb li:nth-child(3) a::text").get(default="").strip()
    return value or None


def _currency(price_raw: str) -> str | None:
    for symbol, code in {"£": "GBP", "$": "USD", "€": "EUR"}.items():
        if symbol in price_raw:
            return code
    return None


def _stock_count(parts: list[str]) -> int | None:
    for part in parts:
        digits = "".join(ch for ch in part if ch.isdigit())
        if digits:
            return int(digits)
    return None


def _table_value(response, label: str) -> str | None:
    for row in response.css("table.table.table-striped tr"):
        header = row.css("th::text").get(default="").strip()
        if header == label:
            return row.css("td::text").get(default="").strip() or None
    return None

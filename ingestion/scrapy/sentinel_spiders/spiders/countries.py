"""Scrape This Site — countries and hockey teams.

Both datasets live on one sandbox domain. Countries gives the warehouse a clean
dimensional fact (population, area) to model; hockey gives a second, differently shaped
table so the models are not written against a single happy path.
"""

from __future__ import annotations

from datetime import datetime, timezone

import scrapy

from ingestion.scrapy.sentinel_spiders.items import CountryItem, HockeyTeamItem

BASE = "https://www.scrapethissite.com"


class CountriesSpider(scrapy.Spider):
    name = "countries"
    allowed_domains = ["scrapethissite.com", "www.scrapethissite.com"]
    start_urls = [f"{BASE}/pages/simple/"]

    custom_settings = {"DOWNLOAD_DELAY": 1.0}

    def parse(self, response):
        """Extract the country table.

        @url https://www.scrapethissite.com/pages/simple/
        @returns items 100
        @scrapes source url name capital population area_km2
        """
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        for block in response.css("div.country"):
            name = _text(block, "h3.country-name")
            capital = _text(block, "span.country-capital")
            population = _text(block, "span.country-population")
            area = _text(block, "span.country-area")
            if not name:
                continue
            yield CountryItem(
                source="countries",
                url=response.url,
                name=name,
                capital=capital,
                population=_num(population),
                area_km2=_num(area),
                scraped_at=now,
            )


class HockeySpider(scrapy.Spider):
    name = "hockey"
    allowed_domains = ["scrapethissite.com", "www.scrapethissite.com"]
    start_urls = [f"{BASE}/pages/forms/"]

    custom_settings = {
        "DOWNLOAD_DELAY": 1.0,
        "CLOSESPIDER_PAGECOUNT": 12,
    }

    def parse(self, response):
        """Extract one page of the team table, then walk the year pages.

        @url https://www.scrapethissite.com/pages/forms/
        @returns items 25
        @scrapes source url year team wins losses win_pct
        """
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        for row in response.css("table.table tr.team"):
            team = _text(row, "td.name")
            if not team:
                continue
            yield HockeyTeamItem(
                source="countries",  # both live on the approved scrapethissite.com entry
                url=response.url,
                year=_num(_text(row, "td.year")),
                team=team,
                wins=_num(_text(row, "td.wins")),
                losses=_num(_text(row, "td.losses")),
                ot_losses=_num(_text(row, "td.ot-losses")),
                win_pct=_num(_text(row, "td.pct")),
                goals_for=_num(_text(row, "td.gf")),
                goals_against=_num(_text(row, "td.ga")),
                scraped_at=now,
            )

        next_page = response.css("ul.pagination a[aria-label='Next']::attr(href)").get()
        if next_page:
            yield response.follow(next_page, callback=self.parse)


def _text(node, selector: str) -> str | None:
    value = " ".join(part.strip() for part in node.css(f"{selector}::text").getall() if part.strip())
    return value or None


def _num(value: str | None):
    if value is None:
        return None
    cleaned = value.replace(",", "").replace("%", "").strip()
    try:
        number = float(cleaned)
    except ValueError:
        return None
    return int(number) if number.is_integer() else number

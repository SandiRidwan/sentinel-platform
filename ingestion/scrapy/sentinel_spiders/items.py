"""Item definitions — the contract between a spider and the rest of the platform.

Every field here is a field the silver layer and dbt models expect. Adding a field in a
spider that is not declared here is a bug the pipeline will reject, not silently store.
"""

from __future__ import annotations

import scrapy


class BookItem(scrapy.Item):
    source = scrapy.Field()
    url = scrapy.Field()
    title = scrapy.Field()
    price = scrapy.Field()
    currency = scrapy.Field()
    availability = scrapy.Field()
    stock_count = scrapy.Field()
    rating = scrapy.Field()
    category = scrapy.Field()
    upc = scrapy.Field()
    scraped_at = scrapy.Field()


class QuoteItem(scrapy.Item):
    source = scrapy.Field()
    url = scrapy.Field()
    text = scrapy.Field()
    author = scrapy.Field()
    author_url = scrapy.Field()
    tags = scrapy.Field()
    scraped_at = scrapy.Field()


class CountryItem(scrapy.Item):
    source = scrapy.Field()
    url = scrapy.Field()
    name = scrapy.Field()
    capital = scrapy.Field()
    population = scrapy.Field()
    area_km2 = scrapy.Field()
    scraped_at = scrapy.Field()


class HockeyTeamItem(scrapy.Item):
    source = scrapy.Field()
    url = scrapy.Field()
    year = scrapy.Field()
    team = scrapy.Field()
    wins = scrapy.Field()
    losses = scrapy.Field()
    ot_losses = scrapy.Field()
    win_pct = scrapy.Field()
    goals_for = scrapy.Field()
    goals_against = scrapy.Field()
    scraped_at = scrapy.Field()


class CategoryItem(scrapy.Item):
    """A catalogue category. This is the dimension the product fact table joins to."""

    source = scrapy.Field()
    url = scrapy.Field()
    name = scrapy.Field()
    slug = scrapy.Field()
    scraped_at = scrapy.Field()

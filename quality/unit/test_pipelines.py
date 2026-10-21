"""Pipeline behaviour: validation rejects, PII masking works, table names are explicit."""

from __future__ import annotations

import pytest

from ingestion.scrapy.sentinel_spiders.items import BookItem, CountryItem, QuoteItem
from ingestion.scrapy.sentinel_spiders.pipelines import (
    PiiScanPipeline,
    ValidationPipeline,
    _table_name,
)


class FakeSpider:
    name = "test"

    class crawler:  # noqa: N801 - stand-in for scrapy's crawler attribute
        class stats:  # noqa: N801
            @staticmethod
            def set_value(*_args, **_kwargs):
                return None


def test_missing_required_field_is_dropped():
    from scrapy.exceptions import DropItem

    pipeline = ValidationPipeline()
    pipeline.open_spider(FakeSpider())
    item = BookItem(source="books", url="https://books.toscrape.com/x", title=None)

    with pytest.raises(DropItem):
        pipeline.process_item(item, FakeSpider())

    assert pipeline.rejected == 1
    assert pipeline.accepted == 0
    assert pipeline.reasons["missing:title"] == 1


def test_numeric_string_is_coerced_not_stored_as_text():
    pipeline = ValidationPipeline()
    pipeline.open_spider(FakeSpider())
    item = BookItem(
        source="books",
        url="https://books.toscrape.com/x",
        title="A Book",
        price="£51.77",
        currency="GBP",
        availability="In stock",
        rating=3,
    )
    result = pipeline.process_item(item, FakeSpider())
    assert result["price"] == 51.77
    assert pipeline.accepted == 1


def test_unparseable_numeric_is_rejected_not_guessed():
    from scrapy.exceptions import DropItem

    pipeline = ValidationPipeline()
    pipeline.open_spider(FakeSpider())
    item = CountryItem(source="countries", url="u", name="Nowhere", population="unknown")

    with pytest.raises(DropItem):
        pipeline.process_item(item, FakeSpider())

    assert pipeline.reasons["unparseable:population"] == 1


def test_pii_is_masked_by_default(monkeypatch):
    monkeypatch.setenv("PII_MASK_ENABLED", "true")
    pipeline = PiiScanPipeline()
    pipeline.open_spider(FakeSpider())
    item = QuoteItem(
        source="quotes",
        url="u",
        text="write to someone@example.com or +62 812-0000-0000",
        author="A",
    )
    result = pipeline.process_item(item, FakeSpider())
    assert "someone@example.com" not in result["text"]
    assert "[REDACTED_EMAIL]" in result["text"]
    assert pipeline.detections["email"] == 1


def test_pii_detected_but_preserved_when_masking_disabled(monkeypatch):
    monkeypatch.setenv("PII_MASK_ENABLED", "false")
    pipeline = PiiScanPipeline()
    pipeline.open_spider(FakeSpider())
    item = QuoteItem(source="quotes", url="u", text="reach me at a@b.com", author="A")
    result = pipeline.process_item(item, FakeSpider())
    assert "a@b.com" in result["text"]  # preserved
    assert pipeline.detections["email"] == 1  # still counted


@pytest.mark.parametrize(
    ("item_name", "expected"),
    [
        ("BookItem", "books"),
        ("CountryItem", "countries"),  # not "countrys"
        ("HockeyTeamItem", "hockey_teams"),
        ("CategoryItem", "book_categories"),
        ("QuoteItem", "quotes"),
    ],
)
def test_table_names_are_explicit(item_name: str, expected: str):
    assert _table_name(item_name) == expected

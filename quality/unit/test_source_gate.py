"""The source register is the gate. These tests prove it refuses what it should.

If these fail, the platform is scraping things it has not justified. That is a correctness
bug, not a config preference.
"""

from __future__ import annotations

import pytest

from tools.common.sources import (
    SourceNotApproved,
    assert_approved,
    is_path_allowed,
    load_register,
)


@pytest.fixture(autouse=True)
def _fresh_register():
    load_register.cache_clear()
    yield
    load_register.cache_clear()


def test_approved_sources_resolve():
    for host in ("books.toscrape.com", "quotes.toscrape.com", "scrapethissite.com"):
        source = assert_approved(host)
        assert source.approved is True
        assert source.legal_basis


def test_www_prefix_is_the_same_site():
    assert assert_approved("www.scrapethissite.com").key == "countries"
    assert assert_approved("scrapethissite.com").key == "countries"


def test_wikipedia_is_rejected_because_robots_disallows_w_api():
    # en.wikipedia.org robots.txt has "User-agent: *  Disallow: /w/", which covers
    # /w/api.php. This was assumed to be api_permitted in the first draft and was wrong.
    with pytest.raises(SourceNotApproved) as exc:
        assert_approved("en.wikipedia.org")
    assert "rejected" in str(exc.value)


def test_undeclared_host_is_rejected():
    with pytest.raises(SourceNotApproved) as exc:
        assert_approved("example.com")
    assert "not declared" in str(exc.value)


@pytest.mark.parametrize("host", ["linkedin.com", "www.linkedin.com", "facebook.com", "twitter.com"])
def test_tos_hosts_are_rejected(host: str):
    with pytest.raises(SourceNotApproved):
        assert_approved(host)


def test_robots_txt_is_always_allowed():
    # Refusing the file that states the rules would be absurd.
    assert is_path_allowed("scrapethissite.com", "/robots.txt") is True
    assert is_path_allowed("example.com", "/robots.txt") is True


def test_path_outside_allowlist_is_refused():
    assert is_path_allowed("scrapethissite.com", "/pages/simple/") is True
    assert is_path_allowed("scrapethissite.com", "/lessons/") is False
    assert is_path_allowed("scrapethissite.com", "/faq/") is False


def test_register_records_what_was_checked():
    register = load_register()
    rejected_hosts = {r.host for r in register.rejected}
    assert "en.wikipedia.org" in rejected_hosts
    assert "openlibrary.org" in rejected_hosts
    assert register.reviewed_at  # a register with no review date is not a register

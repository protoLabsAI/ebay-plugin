"""Amazon as a second source. Fixtures are REAL strings from a live results page (2026-08-05).

The point of this file is the two ways an Amazon number differs from an eBay one: it is an
ASKING price and never a sale price, and some of the rows are paid ads.
"""

from __future__ import annotations

import pytest
from ebay_plugin.amazon import RESULT_JS, normalize, search_url
from ebay_plugin.extract import Listing, summarize


class TestSearchUrl:
    def test_builds_a_search(self):
        assert search_url("switch oled").startswith("https://www.amazon.com/s?")
        assert "k=switch+oled" in search_url("switch oled")

    def test_sort_and_price_bounds(self):
        url = search_url("x", sort="price_low", min_price=10, max_price=99)
        assert "s=price-asc-rank" in url and "low-price=10" in url and "high-price=99" in url

    def test_relevance_sort_adds_no_key(self):
        assert "s=" not in search_url("x")

    def test_domain_selects_the_marketplace(self):
        assert search_url("x", domain="www.amazon.co.uk").startswith("https://www.amazon.co.uk/")

    def test_blank_query_is_refused(self):
        with pytest.raises(ValueError):
            search_url("  ")


class TestNormalize:
    def _row(self, **kw):
        base = {
            "title": "Nintendo Switch – OLED Model w/White Joy-Con",
            "url": "https://www.amazon.com/dp/B098RKWHHZ",
            "price": "$379.00",
            "rating": "4.7 out of 5 stars",
            "delivery": "FREE delivery Fri, Aug 7",
        }
        base.update(kw)
        return base

    def test_reads_the_live_shapes(self):
        rows, dropped = normalize([self._row()])
        assert dropped == 0
        listing = rows[0]
        assert listing.price == 379.00 and listing.currency == "$"
        assert listing.rating == 4.7
        assert listing.shipping == 0.0  # "FREE delivery" is knowable

    def test_amazon_rows_are_never_marked_sold(self):
        """Amazon publishes no sale history. An asking price must never be able to travel
        through the system as a comp."""
        rows, _ = normalize([self._row()])
        assert rows[0].sold is False and rows[0].source == "amazon"

    def test_unstated_delivery_cost_is_unknown_not_free(self):
        """Amazon usually hides a paid delivery figure until checkout. Guessing zero would
        silently favour the listing."""
        rows, _ = normalize([self._row(delivery="Delivery Fri, Aug 7")])
        assert rows[0].shipping is None

    def test_priceless_rows_are_dropped_and_counted(self):
        rows, dropped = normalize([self._row(price=""), self._row()])
        assert len(rows) == 1 and dropped == 1

    def test_a_missing_rating_is_none_not_zero(self):
        """Zero stars is a real, terrible rating. Unknown must not look like it."""
        rows, _ = normalize([self._row(rating="")])
        assert rows[0].rating is None

    def test_sponsored_is_carried_through(self):
        rows, _ = normalize([self._row(sponsored=True)])
        assert rows[0].sponsored is True


class TestSponsoredExclusion:
    def _l(self, price, sponsored=False):
        return Listing("t", "u", price, None, "$", 0.0, "", "", False, "amazon", None, sponsored)

    def test_ads_are_excluded_from_the_statistics(self):
        """Amazon salts sponsored rows into every page. Letting a seller's ad spend into the
        median makes the market look pricier than it is."""
        s = summarize([self._l(100), self._l(110), self._l(900, sponsored=True)])
        assert s["count"] == 2 and s["high"] == 110 and s["sponsored_excluded"] == 1

    def test_the_exclusion_is_reported_not_silent(self):
        s = summarize([self._l(100), self._l(900, sponsored=True)])
        assert "sponsored_excluded" in s

    def test_no_ads_means_no_noise_in_the_output(self):
        assert "sponsored_excluded" not in summarize([self._l(100), self._l(110)])

    def test_an_all_sponsored_page_is_empty_not_averaged(self):
        s = summarize([self._l(900, sponsored=True)])
        assert s["count"] == 0 and s["sponsored_excluded"] == 1


class TestPageScript:
    def test_detects_amazons_bot_wall(self):
        """Amazon's robot check renders a card-less page — identical to "no results" unless
        it is named."""
        for marker in ("captcha", "robot check", "automated access"):
            assert marker in RESULT_JS.lower()

    def test_reads_the_offscreen_price_not_the_split_one(self):
        """`.a-price-whole` renders as "379." and loses the cents; `.a-offscreen` carries the
        assembled amount."""
        assert "a-offscreen" in RESULT_JS

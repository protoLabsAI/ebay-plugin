"""Money and row handling — the part that decides whether a price is right or invented.

The fixtures here are REAL strings captured from a live eBay results page on 2026-08-05, not
imagined ones. Recalled markup is what this plugin exists to avoid: the first draft used
`.s-item__shipping` from memory, which matches nothing on eBay's current `s-card` layout —
it would have silently reported every listing as free-shipping and compared a $10-plus-$15
item against a $20-shipped one as if the first were cheaper.
"""

from __future__ import annotations

import pytest
from ebay_plugin.extract import (
    Listing,
    clean_title,
    normalize,
    parse_money,
    search_url,
    summarize,
)


class TestParseMoney:
    @pytest.mark.parametrize(
        "text,low,high",
        [
            ("$284.99", 284.99, None),
            ("+$12.99 delivery", 12.99, None),  # live shipping string
            ("$24.99 to $39.99", 24.99, 39.99),  # range keeps BOTH ends
            ("C $1,299.00", 1299.00, None),  # thousands separator
            ("£12.50", 12.50, None),
            ("", None, None),
            ("Free delivery", None, None),  # no number → unknown, NOT zero
            ("or Best Offer", None, None),
        ],
    )
    def test_values(self, text, low, high):
        got_low, got_high, _ = parse_money(text)
        assert got_low == low and got_high == high

    def test_currency_is_captured(self):
        assert parse_money("C $1,299.00")[2].endswith("$")
        assert parse_money("£12.50")[2] == "£"

    def test_european_decimal_comma(self):
        """1.299,00 is twelve hundred, not 1.30. Deciding by which separator comes LAST is
        what keeps a €1,299 item from being priced as €1.30."""
        assert parse_money("1.299,00")[0] == 1299.00
        assert parse_money("1,299.00")[0] == 1299.00
        assert parse_money("9,99")[0] == 9.99  # lone comma, 2dp → decimal
        assert parse_money("1,299")[0] == 1299.0  # lone comma, 3dp → thousands

    def test_unparseable_never_becomes_zero(self):
        """A phantom $0 comp drags an average down and reads as a real data point."""
        assert parse_money("Best offer accepted")[0] is None


class TestNormalize:
    def _row(self, **kw):
        base = {"title": "Nintendo Switch OLED", "url": "https://www.ebay.com/itm/1", "price": "$284.99"}
        base.update(kw)
        return base

    def test_strips_the_chrome_ebay_bakes_into_titles(self):
        """Live titles carry a screen-reader suffix and a 'New Listing' prefix; both leak
        into anything the agent shows or compares."""
        raw = "New ListingNintendo Switch OLED Zelda EditionOpens in a new window or tab"
        assert clean_title(raw) == "Nintendo Switch OLED Zelda Edition"

    def test_priceless_rows_are_dropped_and_counted(self):
        rows = [self._row(), self._row(price="or Best Offer"), self._row(price="")]
        listings, dropped = normalize(rows, sold=True)
        assert len(listings) == 1 and dropped == 2

    def test_free_shipping_is_zero_not_unknown(self):
        listings, _ = normalize([self._row(shipping="Free delivery")], sold=True)
        assert listings[0].shipping == 0.0

    def test_absent_shipping_stays_unknown(self):
        """Unknown must not collapse to free — that silently favours the listing."""
        listings, _ = normalize([self._row(shipping="")], sold=True)
        assert listings[0].shipping is None

    def test_sold_flag_rides_every_row(self):
        """The whole point: an asking price must never be reportable as a sale price."""
        sold, _ = normalize([self._row()], sold=True)
        active, _ = normalize([self._row()], sold=False)
        assert sold[0].sold is True and active[0].sold is False

    def test_untitled_rows_are_dropped(self):
        _, dropped = normalize([self._row(title="")], sold=True)
        assert dropped == 1


class TestSummarize:
    def _l(self, price, shipping=0.0):
        return Listing("t", "u", price, None, "$", shipping, "", "", True)

    def test_median_resists_an_outlier_that_drags_the_mean(self):
        """A lot-of-50 listing next to single units is the normal case, not the edge one."""
        s = summarize([self._l(20), self._l(21), self._l(22), self._l(23), self._l(2000)])
        assert s["median"] == 22
        assert s["mean"] > 400  # the mean chased the outlier; the median didn't

    def test_shipping_is_included_in_the_comparison(self):
        """$10 + $15 shipping loses to $20 shipped free; comparing item prices inverts it."""
        s = summarize([self._l(10, 15), self._l(20, 0)])
        assert s["low"] == 20 and s["high"] == 25

    def test_even_count_medians_average_the_middle_pair(self):
        assert summarize([self._l(10), self._l(20), self._l(30), self._l(40)])["median"] == 25

    def test_empty_is_a_count_not_a_zero_price(self):
        assert summarize([]) == {"count": 0}


class TestSearchUrl:
    def test_sold_view_sets_both_filters(self):
        """LH_Sold alone is not the sold view — both are needed for completed sales."""
        url = search_url("switch oled", sold=True)
        assert "LH_Sold=1" in url and "LH_Complete=1" in url

    def test_active_view_sets_neither(self):
        assert "LH_Sold" not in search_url("switch oled", sold=False)

    def test_domain_selects_the_marketplace(self):
        assert search_url("x", domain="www.ebay.co.uk").startswith("https://www.ebay.co.uk/")

    def test_price_bounds_and_condition(self):
        url = search_url("x", condition="used", min_price=10, max_price=99.5)
        assert "LH_ItemCondition=4" in url and "_udlo=10" in url and "_udhi=99.5" in url

    def test_blank_query_is_refused(self):
        with pytest.raises(ValueError):
            search_url("   ")

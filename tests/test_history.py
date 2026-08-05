"""The self-recorded price log. Its whole value rests on being honest about being thin."""

from __future__ import annotations

import pytest
from ebay_plugin.extract import Listing
from ebay_plugin.history import PriceHistory, normalize_key, position


def _l(price, shipping=0.0, sponsored=False):
    return Listing("t", "u", price, None, "$", shipping, "", "", False, "amazon", None, sponsored)


@pytest.fixture
def store(tmp_path):
    return PriceHistory(tmp_path / "prices.db")


class TestRecording:
    def test_records_price_plus_shipping(self, store):
        assert store.record("switch oled", "amazon", [_l(100, 10)]) == 1
        assert store.summary("switch oled")["low"] == 110.0

    def test_ads_are_kept_out_of_the_baseline(self, store):
        """Recording a sponsored price would bake a seller's ad spend into the historical
        baseline, so a later "cheapest we've seen" is measured against a price nobody charged."""
        assert store.record("k", "amazon", [_l(100), _l(900, sponsored=True)]) == 1
        assert store.summary("k")["high"] == 100.0

    def test_priceless_rows_are_skipped(self, store):
        assert store.record("k", "amazon", [_l(None)]) == 0

    def test_a_rephrased_query_hits_the_same_history(self, store):
        """Otherwise a rephrase silently starts an empty record and the agent reports "no
        history" for something it has watched for weeks."""
        store.record("Nintendo Switch OLED", "amazon", [_l(100)])
        assert store.summary("nintendo  switch   oled")["observations"] == 1

    def test_source_filter(self, store):
        store.record("k", "amazon", [_l(100)])
        store.record("k", "ebay", [_l(200)])
        assert store.summary("k", source="ebay")["low"] == 200.0
        assert store.summary("k")["observations"] == 2

    def test_normalize_key_is_stable(self):
        assert normalize_key("  A  b ") == "a b"


class TestSummary:
    def test_empty_is_explicitly_empty(self, store):
        """Silence must not read as stability."""
        assert store.summary("never-searched") == {"observations": 0, "window_days": 90}

    def test_window_excludes_older_observations(self, store):
        store.record("k", "amazon", [_l(100)], now="2020-01-01T00:00:00+00:00")
        assert store.summary("k", days=30)["observations"] == 0


class TestPosition:
    def _hist(self, n=20, days=5, low=100, high=200, median=150):
        return {"observations": n, "distinct_days": days, "low": low, "high": high, "median": median}

    def test_no_history_says_so_instead_of_guessing(self):
        assert "no history yet" in position(120, {"observations": 0})["verdict"]

    def test_thin_history_refuses_to_call_a_trend(self):
        """A percentile off three observations is arithmetic theatre and lends the number
        authority it hasn't earned."""
        p = position(120, self._hist(n=3, days=1))
        assert "too thin" in p["verdict"] and "percentile" not in p

    def test_a_real_record_places_the_price(self):
        p = position(120, self._hist())
        assert p["percentile"] == 20.0 and "below the recorded median" in p["verdict"]

    def test_the_extremes_are_named(self):
        assert "lowest recorded" in position(100, self._hist())["verdict"]
        assert "highest recorded" in position(200, self._hist())["verdict"]

    def test_a_flat_record_does_not_divide_by_zero(self):
        p = position(100, self._hist(low=100, high=100, median=100))
        assert p["percentile"] == 0.0

    def test_deltas_are_reported(self):
        p = position(120, self._hist())
        assert p["vs_low"] == 20.0 and p["vs_median"] == -30.0

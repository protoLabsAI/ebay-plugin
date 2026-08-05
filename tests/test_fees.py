"""Seller economics. Arithmetic must be exact and the schedule's provenance must be honest.

A margin that's quietly wrong is worse than no margin at all — it gets acted on.
"""

from __future__ import annotations

import pytest
from ebay_plugin.fees import (
    PAYPAL_GOODS_SERVICES,
    Costs,
    FeeSchedule,
    breakeven_price,
    net_proceeds,
    schedule_from_config,
)

#: A round schedule so expected values are checkable by hand.
ROUND = FeeSchedule(
    final_value_pct=10.0,
    per_order_fee=0.50,
    per_order_fee_small=0.25,
    small_order_threshold=10.0,
    international_pct=2.0,
    verified_on="2026-08-05",
)


class TestNetProceeds:
    def test_the_basic_shape(self):
        r = net_proceeds(sale_price=100.0, schedule=ROUND)
        assert r["revenue"] == 100.0
        assert r["fees"]["final_value"] == 10.0
        assert r["fees"]["per_order"] == 0.50
        assert r["net"] == 89.50

    def test_shipping_charged_is_revenue_and_is_fee_bearing(self):
        """Both halves matter: it's money in, and eBay charges on it."""
        r = net_proceeds(sale_price=100.0, shipping_charged=10.0, schedule=ROUND)
        assert r["revenue"] == 110.0
        assert r["fees"]["final_value"] == 11.0

    def test_sales_tax_raises_fees_without_raising_revenue(self):
        """THE subtlety naive calculators miss: eBay computes its fee on a total including
        tax the seller never receives, so the cut grows while the takings don't."""
        plain = net_proceeds(sale_price=100.0, schedule=ROUND)
        taxed = net_proceeds(sale_price=100.0, sales_tax=8.0, schedule=ROUND)
        assert taxed["revenue"] == plain["revenue"] == 100.0
        assert taxed["fees"]["final_value"] == 10.80 > plain["fees"]["final_value"]
        assert taxed["net"] < plain["net"]

    def test_free_shipping_is_not_free(self):
        """Absorbing postage moves the cost to you; eBay still charges on what the buyer paid."""
        charged = net_proceeds(sale_price=100.0, shipping_charged=10.0, schedule=ROUND, costs=Costs(shipping_cost=10.0))
        absorbed = net_proceeds(sale_price=100.0, schedule=ROUND, costs=Costs(shipping_cost=10.0))
        assert absorbed["net"] < charged["net"]

    def test_small_order_uses_the_lower_fixed_fee(self):
        assert net_proceeds(sale_price=5.0, schedule=ROUND)["fees"]["per_order"] == 0.25
        assert net_proceeds(sale_price=50.0, schedule=ROUND)["fees"]["per_order"] == 0.50

    def test_international_and_promoted_only_apply_when_asked(self):
        base = net_proceeds(sale_price=100.0, schedule=ROUND)
        assert base["fees"]["international"] == 0.0 and base["fees"]["promoted"] == 0.0
        intl = net_proceeds(sale_price=100.0, international=True, schedule=ROUND)
        assert intl["fees"]["international"] == 2.0

    def test_promoted_defaults_off_so_costs_are_not_overstated(self):
        s = FeeSchedule(promoted_pct=5.0, verified_on="x")
        assert net_proceeds(sale_price=100.0, schedule=s)["fees"]["promoted"] == 0.0
        assert net_proceeds(sale_price=100.0, promoted=True, schedule=s)["fees"]["promoted"] == 5.0

    def test_labor_is_a_real_cost(self):
        r = net_proceeds(sale_price=100.0, schedule=ROUND, costs=Costs(labor_hours=0.5, hourly_rate=20.0))
        assert r["costs"]["labor"] == 10.0 and r["net"] == 79.50

    def test_a_loss_is_reported_as_a_loss(self):
        r = net_proceeds(sale_price=10.0, schedule=ROUND, costs=Costs(item_cost=20.0))
        assert r["net"] < 0 and r["margin_pct"] < 0

    def test_payment_processing_is_zero_on_ebay_by_default(self):
        """Managed payments bundles it into the final value fee; charging it again would
        double-count every sale."""
        assert net_proceeds(sale_price=100.0, schedule=ROUND)["fees"]["payment_processing"] == 0.0

    def test_an_offebay_processor_can_be_modelled(self):
        r = net_proceeds(sale_price=100.0, schedule=PAYPAL_GOODS_SERVICES)
        assert r["fees"]["payment_processing"] == pytest.approx(3.48, abs=0.01)
        assert r["fees"]["final_value"] == 0.0  # not an eBay sale

    def test_negative_amounts_are_refused(self):
        with pytest.raises(ValueError):
            net_proceeds(sale_price=-1.0)


class TestProvenance:
    def test_the_shipped_default_is_marked_unverified(self):
        """Rates change and vary by category. The default is a starting point, not a fact,
        and every result has to say so."""
        r = net_proceeds(sale_price=100.0)
        assert r["schedule"]["verified"] is False
        assert "UNVERIFIED" in r["caveat"]

    def test_a_verified_schedule_drops_the_caveat(self):
        r = net_proceeds(sale_price=100.0, schedule=ROUND)
        assert r["schedule"]["verified"] is True and r["caveat"] == ""

    def test_config_overlays_and_ignores_junk(self):
        s = schedule_from_config({"final_value_pct": 12.9, "verified_on": "2026-08-05", "nonsense": 1})
        assert s.final_value_pct == 12.9 and s.verified is True
        assert not hasattr(s, "nonsense")

    def test_unparseable_config_values_fall_back_to_the_default(self):
        s = schedule_from_config({"final_value_pct": "not-a-number"})
        assert s.final_value_pct == FeeSchedule().final_value_pct

    def test_empty_config_is_the_default(self):
        assert schedule_from_config(None) == FeeSchedule()


class TestBreakeven:
    def test_breakeven_nets_exactly_zero(self):
        """The property that matters — solved algebraically, so verify by round-tripping it
        back through the fee calculation."""
        costs = Costs(item_cost=8.0, shipping_cost=5.0)
        price = breakeven_price(schedule=ROUND, costs=costs)
        assert net_proceeds(sale_price=price, schedule=ROUND, costs=costs)["net"] == pytest.approx(0.0, abs=0.02)

    def test_breakeven_rises_with_costs(self):
        low = breakeven_price(schedule=ROUND, costs=Costs(item_cost=5.0))
        high = breakeven_price(schedule=ROUND, costs=Costs(item_cost=50.0))
        assert high > low

    def test_charging_shipping_lowers_the_item_floor(self):
        absorbed = breakeven_price(schedule=ROUND, costs=Costs(shipping_cost=10.0))
        charged = breakeven_price(schedule=ROUND, costs=Costs(shipping_cost=10.0), shipping_charged=10.0)
        assert charged < absorbed

    def test_zero_cost_still_has_a_floor_because_fees_are_not_zero(self):
        assert breakeven_price(schedule=ROUND) > 0

    def test_an_impossible_rate_fails_loudly(self):
        with pytest.raises(ValueError):
            breakeven_price(schedule=FeeSchedule(final_value_pct=100.0))

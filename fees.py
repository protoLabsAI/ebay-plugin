"""Seller economics: what actually lands in your pocket after eBay takes its cut.

Pure arithmetic — no I/O, so every edge can be tested exactly.

**The rates are config, not knowledge.** Fee schedules change, vary by category, store
subscription, and country, and an agent that recalls "about 13%" and states it as fact is
exactly the failure this plugin is built to avoid. So :class:`FeeSchedule` carries its own
``source`` and ``verified_on``, every result reports the schedule it used, and the shipped
default is a starting point the operator confirms against their own eBay invoice — not a
truth claim.

Two subtleties that make naive calculators wrong, both modelled here:

1. **The fee base includes sales tax the seller never receives.** eBay collects and remits
   the tax, but computes the final value fee on a total that includes it. Fees rise while
   revenue doesn't, so ignoring it understates the cut.
2. **Shipping charged to the buyer is revenue AND is fee-bearing.** Free shipping isn't
   free: it moves the cost to you while eBay still charges on the total the buyer paid.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace

#: Shipped defaults for a US seller without a store subscription, from public 2026 fee
#: summaries — NOT from eBay's own fee page, which is why ``verified_on`` is blank and every
#: result says so. Confirm against a real eBay invoice, then set these in plugin config.
DEFAULT_SCHEDULE_SOURCE = (
    "public 2026 fee summaries (US, no store subscription) — UNVERIFIED against eBay's own "
    "fee page or a real invoice; confirm before trusting a margin to the cent"
)


@dataclass(frozen=True)
class FeeSchedule:
    """A marketplace's cut. Percentages are whole numbers: ``13.6`` means 13.6%."""

    final_value_pct: float = 13.6
    #: Fixed per-order fee. eBay charges a lower one on small orders.
    per_order_fee: float = 0.40
    per_order_fee_small: float = 0.30
    small_order_threshold: float = 10.0
    #: Charged when the buyer is outside the seller's country.
    international_pct: float = 1.65
    #: Promoted Listings ad rate. Only applies if the sale came through a promoted listing,
    #: so it defaults off — silently baking in an ad rate would overstate every cost.
    promoted_pct: float = 0.0
    #: Separate payment processing. Zero on eBay: managed payments bundles it into the final
    #: value fee. Non-zero only for an off-eBay processor (e.g. PayPal taken directly).
    payment_pct: float = 0.0
    payment_fixed: float = 0.0
    source: str = DEFAULT_SCHEDULE_SOURCE
    #: ISO date the operator last checked these against reality. Blank = never.
    verified_on: str = ""

    @property
    def verified(self) -> bool:
        return bool(self.verified_on.strip())

    def as_dict(self) -> dict:
        d = asdict(self)
        d["verified"] = self.verified
        return d


def schedule_from_config(cfg: dict | None) -> FeeSchedule:
    """Overlay operator config onto the defaults, ignoring unknown keys."""
    base = FeeSchedule()
    if not cfg:
        return base
    known = {f for f in base.__dataclass_fields__}  # noqa: SIM118
    updates = {k: v for k, v in cfg.items() if k in known and v is not None}
    for k in ("source", "verified_on"):
        if k in updates:
            updates[k] = str(updates[k])
    for k, v in list(updates.items()):
        if k not in ("source", "verified_on"):
            try:
                updates[k] = float(v)
            except (TypeError, ValueError):
                del updates[k]
    return replace(base, **updates)


@dataclass
class Costs:
    """What the sale costs YOU, as opposed to what the marketplace takes."""

    item_cost: float = 0.0
    #: What shipping actually costs you — postage, packaging. Not what you charged.
    shipping_cost: float = 0.0
    other: float = 0.0
    labor_hours: float = 0.0
    hourly_rate: float = 0.0

    @property
    def labor(self) -> float:
        return round(self.labor_hours * self.hourly_rate, 2)

    @property
    def total(self) -> float:
        return round(self.item_cost + self.shipping_cost + self.other + self.labor, 2)


def net_proceeds(
    *,
    sale_price: float,
    shipping_charged: float = 0.0,
    sales_tax: float = 0.0,
    international: bool = False,
    promoted: bool = False,
    schedule: FeeSchedule | None = None,
    costs: Costs | None = None,
) -> dict:
    """Itemise one sale from list price to net profit.

    ``sales_tax`` is what eBay collected from the buyer. It is added to the FEE BASE but
    never to your revenue — you never receive it, yet you are charged on it.
    """
    s = schedule or FeeSchedule()
    c = costs or Costs()
    if sale_price < 0 or shipping_charged < 0 or sales_tax < 0:
        raise ValueError("amounts cannot be negative")

    revenue = round(sale_price + shipping_charged, 2)
    fee_base = round(revenue + sales_tax, 2)

    final_value = _pct(fee_base, s.final_value_pct)
    per_order = s.per_order_fee_small if fee_base <= s.small_order_threshold else s.per_order_fee
    intl = _pct(fee_base, s.international_pct) if international else 0.0
    ad = _pct(fee_base, s.promoted_pct) if promoted else 0.0
    processing = round(_pct(revenue, s.payment_pct) + (s.payment_fixed if s.payment_pct or s.payment_fixed else 0.0), 2)

    fees_total = round(final_value + per_order + intl + ad + processing, 2)
    net = round(revenue - fees_total - c.total, 2)

    return {
        "revenue": revenue,
        "fee_base": fee_base,
        "fee_base_note": (
            "includes sales tax you never receive but are charged on" if sales_tax else "item + shipping charged"
        ),
        "fees": {
            "final_value": final_value,
            "per_order": round(per_order, 2),
            "international": intl,
            "promoted": ad,
            "payment_processing": processing,
            "total": fees_total,
        },
        "costs": {
            "item": round(c.item_cost, 2),
            "shipping": round(c.shipping_cost, 2),
            "labor": c.labor,
            "other": round(c.other, 2),
            "total": c.total,
        },
        "net": net,
        "margin_pct": round(net / revenue * 100, 1) if revenue else 0.0,
        "take_rate_pct": round(fees_total / revenue * 100, 1) if revenue else 0.0,
        "schedule": s.as_dict(),
        "caveat": (
            ""
            if s.verified
            else "Fee schedule is UNVERIFIED — rates vary by category, store subscription and "
            "country. Confirm against a real eBay invoice before trusting this to the cent."
        ),
    }


def breakeven_price(
    *,
    schedule: FeeSchedule | None = None,
    costs: Costs | None = None,
    shipping_charged: float = 0.0,
    international: bool = False,
    promoted: bool = False,
) -> float:
    """The sale price at which net profit is exactly zero.

    Solved, not searched. With ``r`` the combined percentage rate, revenue ``p + shipping``
    and fixed fee ``f``::

        (p + ship) - r·(p + ship) - f - costs = 0
        p = (f + costs) / (1 - r) - ship

    Sales tax is excluded: it varies by buyer location, so a breakeven that assumed one
    would be wrong for most buyers. Real fees will be a little higher wherever tax applies.
    """
    s = schedule or FeeSchedule()
    c = costs or Costs()
    rate = (
        s.final_value_pct
        + (s.international_pct if international else 0.0)
        + (s.promoted_pct if promoted else 0.0)
        + s.payment_pct
    ) / 100.0
    if rate >= 1:
        raise ValueError("fee rate of 100% or more has no breakeven")
    fixed = s.per_order_fee + s.payment_fixed
    price = (fixed + c.total) / (1 - rate) - shipping_charged
    return round(max(price, 0.0), 2)


def _pct(amount: float, pct: float) -> float:
    return round(amount * pct / 100.0, 2)


#: A PayPal-style processor for sales taken OUTSIDE eBay. On eBay itself managed payments
#: bundles processing into the final value fee, so adding this to an eBay sale double-counts.
PAYPAL_GOODS_SERVICES = FeeSchedule(
    final_value_pct=0.0,
    per_order_fee=0.0,
    per_order_fee_small=0.0,
    international_pct=0.0,
    payment_pct=2.99,
    payment_fixed=0.49,
    source="public 2026 PayPal goods-and-services summaries (US domestic) — UNVERIFIED",
    verified_on="",
)

_ = field  # re-exported for callers building schedules dynamically

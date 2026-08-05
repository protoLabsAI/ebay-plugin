"""The eBay tools, built on a browser session rather than the API.

Why a browser at all: the eBay data a seller actually prices against — **what items sold
for** — lives behind the Marketplace Insights API, which eBay lists as Limited Release and
closed to new applicants. The same data is plainly visible in the normal web UI to a
signed-in user. So this drives a real signed-in browser instead.

What that costs, stated up front because it shapes every tool here:

* **Sign-in is required.** eBay serves the sold view only to signed-in users; signed out it
  redirects to a login wall. Every tool checks and says so rather than reporting an empty
  result set.
* **Headless is refused.** eBay answers headless browsers with an error page. The session
  must be headed with a persistent profile — which is also how the sign-in survives.
* **Markup is not a contract.** Selectors were verified live against both current layouts,
  and will eventually break. Every extractor distinguishes "no results" from "couldn't
  read the page" and raises on the second, because a silent empty list becomes "nothing
  sells at that price" — a confidently wrong answer, which is worse than an error.

Nothing here tries to defeat bot detection: no proxy rotation, no fingerprint spoofing, no
CAPTCHA solving. It drives a normal browser at a human pace against the operator's own
account and publicly visible listings. When eBay interposes a challenge, the tools report it
and stop.
"""

from __future__ import annotations

import json
import logging
import time

from .browser import Browser, BrowserError
from .extract import RESULT_JS, SESSION_JS, normalize, search_url, summarize
from .fees import Costs, breakeven_price, net_proceeds, schedule_from_config

log = logging.getLogger("protoagent.plugins.ebay")

#: Rows returned inline to the model. The full set still drives the statistics — this caps
#: only what lands in the context window (ADR 0005).
_SAMPLE = 8


class EbayError(RuntimeError):
    """An operator-facing failure. Message is safe to show verbatim."""


#: What "the results loaded" looks like — either result layout. Waited for after every
#: navigation so a slow redirect chain isn't mistaken for broken markup.
_RESULTS_SELECTOR = "li.s-card, li.s-item"

#: Seconds between re-reads of a page that showed no results AND no reason.
_SETTLE_S = 2.0
#: How many times to re-read before concluding the page really has nothing to say. eBay's
#: gate chain (search → captcha → signin) can take the better part of ten seconds to
#: settle, and giving up early misreports it as broken markup.
_SETTLE_TRIES = 5


def _is_undecided(data) -> bool:
    """True while the page shows neither results nor a reason — i.e. mid-redirect."""
    return isinstance(data, dict) and not any(
        (data.get("count"), data.get("found_container"), data.get("signin_wall"), data.get("challenge"))
    )


def _fetch(browser: Browser, url: str, *, sold: bool):
    """Navigate and extract, mapping every not-actually-data outcome to a clear error."""
    browser.open(url)
    # `open` returns once the requested URL loads, but eBay bounces a search through a CHAIN
    # — search → /splashui/captcha → signin → back to results — and a read taken mid-chain
    # sees no cards and none of the signals set. That in-between state got reported as "eBay
    # changed its markup, file a bug", sending the operator after a defect while the page was
    # still resolving. Waiting for the results container is exact where a guessed sleep is
    # not: it returns the moment the page is ready, and a timeout is itself informative
    # (the gate pages never render results), so we read and classify either way.
    browser.wait_for(_RESULTS_SELECTOR)
    data = browser.eval_json(RESULT_JS)
    # Belt and braces for the case where the wait timed out but the page was merely slow:
    # only re-read while the page still has nothing at all to say.
    for _ in range(_SETTLE_TRIES):
        if not _is_undecided(data):
            break
        time.sleep(_SETTLE_S)
        data = browser.eval_json(RESULT_JS)
    if not isinstance(data, dict):
        raise EbayError(f"unexpected response while reading {url}")
    if data.get("signin_wall"):
        raise EbayError(
            "eBay redirected to its sign-in page. The sold-listings view needs a signed-in "
            "session — run ebay_session_status, sign in once in the browser window, and retry. "
            "The profile keeps you signed in after that."
        )
    if data.get("challenge"):
        raise EbayError(
            "eBay is asking for human verification (its 'Security Measure' / CAPTCHA page) "
            "instead of returning results. Complete it in the browser window, then retry — "
            "this plugin deliberately hands that to you rather than trying to work around it. "
            "If it keeps recurring, raise ebay.min_interval_s to slow the search cadence."
        )
    if not data.get("found_container"):
        # Name where the browser actually ENDED UP. Without it this error is unfalsifiable —
        # "the markup changed" reads the same whether eBay redesigned the page or simply
        # bounced you somewhere else, and only one of those is a bug to file.
        landed = str(data.get("url") or "").strip()
        where = f" The browser ended up at: {landed}" if landed and landed not in url else ""
        raise EbayError(
            "could not find the results list on the page — either eBay changed its markup "
            "(a plugin bug) or it sent the browser somewhere unexpected. Either way this is "
            "NOT an empty search, and reporting zero results would read as 'nothing "
            f"matches'.{where} Run ebay_page_probe on the URL for the details."
        )
    listings, dropped = normalize(data.get("rows") or [], sold=sold)
    return listings, dropped


def build_tools(cfg: dict):
    from langchain_core.tools import tool

    # ONE Browser for the whole tool set, not one per call. A fresh instance per invocation
    # re-ran ensure_session() every time, which meant the second tool call found the daemon
    # THIS PLUGIN had just started and tripped the "someone else owns the browser" guard on
    # itself — the first search worked and every one after it failed. Sharing the instance
    # also keeps the pacing clock honest across calls, which a per-call object silently reset.
    shared = Browser(
        binary=cfg.get("binary") or "agent-browser",
        session=cfg.get("session") or "ebay",
        profile=cfg.get("profile") or "",
        headed=bool(cfg.get("headed", True)),
        timeout_s=float(cfg.get("timeout_s", 60)),
        min_interval_s=float(cfg.get("min_interval_s", 1.5)),
    )

    def _browser() -> Browser:
        return shared

    domain = cfg.get("domain") or "www.ebay.com"
    max_results = int(cfg.get("max_results", 60))

    @tool
    def ebay_price_check(
        query: str,
        condition: str = "any",
        sold: bool = True,
        limit: int = 0,
    ) -> str:
        """What does this item actually go for on eBay? Returns median/quartile price statistics plus a sample of matching listings.

        Defaults to SOLD listings — what buyers actually paid — which is the number to price
        against. Set sold=False for current asking prices, which say what sellers hope for,
        not what the market pays. The result labels which basis it used; never present an
        asking price as a sale price.

        condition: "any", "new" or "used". Statistics cover every result found; only a
        sample is listed. Prices include stated shipping.
        """
        b = _browser()
        try:
            url = search_url(query, domain=domain, sold=sold, condition=condition)
            listings, dropped = _fetch(b, url, sold=sold)
        except (BrowserError, EbayError) as exc:
            return json.dumps({"ok": False, "error": str(exc)})
        except ValueError as exc:
            return json.dumps({"ok": False, "error": str(exc)})

        cap = limit if limit and limit > 0 else max_results
        listings = listings[:cap]
        stats = summarize(listings)
        return json.dumps(
            {
                "ok": True,
                "query": query,
                "basis": "sold listings — what buyers paid"
                if sold
                else "active listings — asking prices, NOT sale prices",
                "condition": condition,
                "url": url,
                "stats": stats,
                # Surfaced so the model can qualify a thin result instead of treating three
                # comps with the same confidence as sixty.
                "results_found": len(listings),
                "unparseable_rows_skipped": dropped,
                "sample": [x.as_dict() for x in listings[:_SAMPLE]],
            }
        )

    @tool
    def ebay_search(query: str, sort: str = "best_match", condition: str = "any", sold: bool = False) -> str:
        """Search eBay listings and return them as structured rows (title, price, shipping, condition, URL).

        Use ebay_price_check for a pricing decision — it returns statistics. Use this when
        you need the individual listings themselves: to read how competitors word a title,
        check what's bundled, or find a specific item.

        sort: best_match, price_low, price_high, newest, ending_soonest.
        """
        b = _browser()
        try:
            url = search_url(query, domain=domain, sold=sold, sort=sort, condition=condition)
            listings, dropped = _fetch(b, url, sold=sold)
        except (BrowserError, EbayError, ValueError) as exc:
            return json.dumps({"ok": False, "error": str(exc)})
        return json.dumps(
            {
                "ok": True,
                "query": query,
                "sold": sold,
                "url": url,
                "count": len(listings),
                "unparseable_rows_skipped": dropped,
                "listings": [x.as_dict() for x in listings[:max_results]],
            }
        )

    @tool
    def ebay_session_status() -> str:
        """Is the browser signed in to eBay? Check this first when a tool reports a sign-in wall.

        Opens eBay in the browser window and reports whether the session is signed in. If it
        isn't, sign in by hand in that window once — the profile persists it, so this is a
        one-time step.
        """
        b = _browser()
        try:
            b.open(f"https://{domain}")
            data = b.eval_json(SESSION_JS)
        except BrowserError as exc:
            return json.dumps({"ok": False, "error": str(exc)})
        signed_in = bool(isinstance(data, dict) and data.get("signed_in"))
        return json.dumps(
            {
                "ok": True,
                "signed_in": signed_in,
                "greeting": (data or {}).get("greeting", ""),
                "next_step": ""
                if signed_in
                else "Sign in to eBay in the browser window that just opened; the profile keeps you signed in.",
            }
        )

    @tool
    def ebay_page_probe(url: str) -> str:
        """Diagnostic: dump a page's candidate result structures. Use when another eBay tool reports it couldn't read the page.

        Returns what the extractor sees — which card selectors matched, and the raw attribute
        text of the first card — so a markup change can be diagnosed and fixed rather than
        guessed at. Read-only; navigates and reads, changes nothing.
        """
        b = _browser()
        try:
            b.open(url)
            data = b.eval_json(RESULT_JS)
        except BrowserError as exc:
            return json.dumps({"ok": False, "error": str(exc)})
        rows = (data or {}).get("rows") or []
        return json.dumps(
            {
                "ok": True,
                "url": url,
                "found_container": (data or {}).get("found_container"),
                "challenge": (data or {}).get("challenge"),
                "signin_wall": (data or {}).get("signin_wall"),
                "row_count": len(rows),
                "first_row": rows[0] if rows else None,
            }
        )

    fee_cfg = cfg.get("fees") or {}

    def _costs(item_cost, shipping_cost, other_costs, labor_hours, hourly_rate) -> Costs:
        return Costs(
            item_cost=float(item_cost or 0),
            shipping_cost=float(shipping_cost or 0),
            other=float(other_costs or 0),
            labor_hours=float(labor_hours or 0),
            hourly_rate=float(hourly_rate or cfg.get("default_hourly_rate", 0) or 0),
        )

    @tool
    def ebay_net_proceeds(
        sale_price: float,
        item_cost: float = 0.0,
        shipping_charged: float = 0.0,
        shipping_cost: float = 0.0,
        sales_tax: float = 0.0,
        other_costs: float = 0.0,
        labor_hours: float = 0.0,
        hourly_rate: float = 0.0,
        international: bool = False,
        promoted: bool = False,
    ) -> str:
        """What you actually keep on an eBay sale: itemized fees, costs, net profit and margin.

        sale_price is what the buyer pays for the item; shipping_charged is what you charged
        them on top (0 for free shipping); shipping_cost is what postage actually costs you.
        sales_tax is what eBay collected from the buyer — you never receive it, but eBay
        charges its fee on a total that includes it, so pass it when you know it.

        Set international=True for a buyer outside your country and promoted=True if the sale
        came through a Promoted Listing. The result states which fee schedule it used and
        whether that schedule has been verified.
        """
        try:
            result = net_proceeds(
                sale_price=float(sale_price),
                shipping_charged=float(shipping_charged or 0),
                sales_tax=float(sales_tax or 0),
                international=bool(international),
                promoted=bool(promoted),
                schedule=schedule_from_config(fee_cfg),
                costs=_costs(item_cost, shipping_cost, other_costs, labor_hours, hourly_rate),
            )
        except ValueError as exc:
            return json.dumps({"ok": False, "error": str(exc)})
        return json.dumps({"ok": True, **result})

    @tool
    def ebay_breakeven(
        item_cost: float = 0.0,
        shipping_cost: float = 0.0,
        shipping_charged: float = 0.0,
        other_costs: float = 0.0,
        labor_hours: float = 0.0,
        hourly_rate: float = 0.0,
        international: bool = False,
        promoted: bool = False,
    ) -> str:
        """The lowest sale price that doesn't lose money, given your costs and eBay's fees.

        Use this before quoting a floor. Excludes sales tax (it varies by buyer location), so
        real fees are marginally higher wherever tax applies — the floor is a floor, not a
        target.
        """
        try:
            costs = _costs(item_cost, shipping_cost, other_costs, labor_hours, hourly_rate)
            schedule = schedule_from_config(fee_cfg)
            price = breakeven_price(
                schedule=schedule,
                costs=costs,
                shipping_charged=float(shipping_charged or 0),
                international=bool(international),
                promoted=bool(promoted),
            )
        except ValueError as exc:
            return json.dumps({"ok": False, "error": str(exc)})
        return json.dumps(
            {
                "ok": True,
                "breakeven_price": price,
                "your_costs": costs.total,
                "note": "net profit is exactly zero at this price; anything below loses money",
                "schedule": schedule.as_dict(),
                "caveat": ""
                if schedule.verified
                else "Fee schedule is UNVERIFIED — confirm against a real eBay invoice.",
            }
        )

    @tool
    def ebay_price_and_profit(
        query: str,
        item_cost: float = 0.0,
        shipping_cost: float = 0.0,
        condition: str = "any",
        labor_hours: float = 0.0,
        hourly_rate: float = 0.0,
    ) -> str:
        """Price an item against real sold comps AND show what you'd net at those prices. The one-call answer to "what should I list this at?".

        Looks up what the item actually sold for, then runs the market's low / median / high
        through eBay's fees and your costs, so the answer is net profit at each price rather
        than a headline number that fees quietly eat.
        """
        b = _browser()
        try:
            url = search_url(query, domain=domain, sold=True, condition=condition)
            listings, dropped = _fetch(b, url, sold=True)
        except (BrowserError, EbayError, ValueError) as exc:
            return json.dumps({"ok": False, "error": str(exc)})

        listings = listings[:max_results]
        stats = summarize(listings)
        if not stats.get("count"):
            return json.dumps(
                {"ok": True, "query": query, "stats": stats, "note": "no sold comps found — try a broader query"}
            )

        schedule = schedule_from_config(fee_cfg)
        costs = _costs(item_cost, shipping_cost, 0, labor_hours, hourly_rate)
        scenarios = {}
        for label in ("p25", "median", "p75"):
            if (price := stats.get(label)) is None:
                continue
            r = net_proceeds(sale_price=price, schedule=schedule, costs=costs)
            scenarios[label] = {
                "sale_price": price,
                "fees": r["fees"]["total"],
                "net": r["net"],
                "margin_pct": r["margin_pct"],
            }

        return json.dumps(
            {
                "ok": True,
                "query": query,
                "basis": "sold listings — what buyers paid",
                "url": url,
                "stats": stats,
                "results_found": len(listings),
                "unparseable_rows_skipped": dropped,
                "breakeven_price": breakeven_price(schedule=schedule, costs=costs),
                "at_market_prices": scenarios,
                "your_costs": costs.total,
                "schedule": schedule.as_dict(),
                "caveat": ""
                if schedule.verified
                else "Fee schedule is UNVERIFIED — confirm against a real eBay invoice.",
            }
        )

    return [
        ebay_price_check,
        ebay_price_and_profit,
        ebay_net_proceeds,
        ebay_breakeven,
        ebay_search,
        ebay_session_status,
        ebay_page_probe,
    ]

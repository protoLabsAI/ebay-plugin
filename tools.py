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
from urllib.parse import urlparse

from . import amazon
from .browser import Browser, BrowserError
from .extract import RESULT_JS, SESSION_JS, normalize, search_url, summarize
from .fees import Costs, breakeven_price, net_proceeds, schedule_from_config

log = logging.getLogger("protoagent.plugins.ebay")

#: Rows returned inline to the model. The full set still drives the statistics — this caps
#: only what lands in the context window (ADR 0005).
_SAMPLE = 8


def _default_history_db() -> str:
    """Per-instance path for the observation log. Resolved through the host when there is
    one; a plain home-relative path otherwise, so the module imports with no host present."""
    try:
        from infra.paths import instance_paths

        return str(instance_paths().store("marketplace") / "prices.db")
    except Exception:  # noqa: BLE001 — host-free (tests, CLI): fall back to a home path
        from pathlib import Path

        return str(Path("~/.protoagent/marketplace-prices.db").expanduser())


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


def _is_undecided(data, url: str = "") -> bool:
    """True while the page shows neither results nor a reason — i.e. mid-redirect. A read that
    came from a tab not showing the site we asked for is DECIDED: waiting will not change it,
    the tab has to be taken back, so the settle loop must not spin on it."""
    if isinstance(data, dict) and _hijacked(data, url):
        return False
    return isinstance(data, dict) and not any(
        (data.get("count"), data.get("found_container"), data.get("signin_wall"), data.get("challenge"))
    )


#: Amazon's equivalent of the eBay results container.
_AMAZON_SELECTOR = 'div[data-component-type="s-search-result"]'


def _classify(data, url: str, *, marketplace: str) -> None:
    """Raise the right error for a page that carries no results. Shared by both sources
    because both gate the same three ways — sign-in, bot check, or a page we can't read —
    and all three arrive as a card-less DOM."""
    if data.get("signin_wall"):
        raise EbayError(
            f"{marketplace} redirected to its sign-in page. Run the session-status tool, sign in "
            "once in the browser window, and retry. The profile keeps you signed in after that."
        )
    if data.get("challenge"):
        raise EbayError(
            f"{marketplace} is asking for human verification (a CAPTCHA / bot check) instead of "
            "returning results. Complete it in the browser window, then retry — this plugin "
            "deliberately hands that to you rather than trying to work around it. If it keeps "
            "recurring, raise min_interval_s to slow the search cadence."
        )
    if not data.get("found_container"):
        landed = str(data.get("url") or "").strip()
        where = f" The browser ended up at: {landed}" if landed and landed not in url else ""
        raise EbayError(
            f"could not find the results list on the {marketplace} page — either it changed its "
            "markup (a plugin bug) or it sent the browser somewhere unexpected. Either way this "
            "is NOT an empty search, and reporting zero results would read as 'nothing "
            f"matches'.{where} Run the page-probe tool on the URL for the details."
        )


def _settle(browser: Browser, script: str, data, url: str):
    """Re-read while the page is still mid-redirect (see _is_undecided), a bounded number of times."""
    for _ in range(_SETTLE_TRIES):
        if not _is_undecided(data, url):
            break
        time.sleep(_SETTLE_S)
        data = browser.eval_json(script)
    return data


def _read_page(browser: Browser, url: str, script: str, selector: str | None = None, *, settle: bool = True):
    """Navigate, wait, read and settle — the one read path every tool uses.

    If the read came from a tab that is not showing the site we navigated to (Chrome's Gemini
    side panel, or an operator's tab that took the active slot), take the plugin's tab back,
    navigate again and read again through the SAME wait + settle steps. Once: a second
    hijack is reported, not chased."""

    def attempt():
        browser.open(url)
        if selector:
            browser.wait_for(selector)
        data = browser.eval_json(script)
        return _settle(browser, script, data, url) if settle else data

    data = attempt()
    if _hijacked(data, url):
        browser.refocus()
        data = attempt()
    if not isinstance(data, dict):
        raise EbayError(f"unexpected response while reading {url}")
    return data


def _read(browser: Browser, url: str, script: str, selector: str):
    """Navigate, wait for results, and re-read while the page is still mid-redirect."""
    return _read_page(browser, url, script, selector)


def _fetch_amazon(browser: Browser, url: str):
    data = _read(browser, url, amazon.RESULT_JS, _AMAZON_SELECTOR)
    _classify(data, url, marketplace="Amazon")
    return amazon.normalize(data.get("rows") or [])


def _fetch(browser: Browser, url: str, *, sold: bool):
    """Navigate and extract, mapping every not-actually-data outcome to a clear error."""
    # `open` returns once the requested URL loads, but eBay bounces a search through a CHAIN
    # — search → /splashui/captcha → signin → back to results — and a read taken mid-chain
    # sees no cards and none of the signals set. That in-between state got reported as "eBay
    # changed its markup, file a bug", sending the operator after a defect while the page was
    # still resolving. Waiting for the results container is exact where a guessed sleep is
    # not, and the settle loop covers a wait that timed out on a merely slow page. The shared
    # read path also takes the tab back if another tab answered (see _read_page).
    data = _read_page(browser, url, RESULT_JS, _RESULTS_SELECTOR)
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
    return listings, dropped, _page_meta(data)


def _site(host: str) -> str:
    host = (host or "").lower()
    return host[4:] if host.startswith("www.") else host


def _hijacked(data, url: str) -> bool:
    """The script ran in a tab that is not showing the site we navigated to — the Gemini panel,
    about:blank, or an operator's tab that took the active slot. Subdomains of the requested
    site (signin.ebay.com, the /splashui/ challenge pages) are the site. A read without a url
    (older payloads, test stubs) is not judged."""
    if not isinstance(data, dict) or not data.get("url") or not url:
        return False
    want = _site(urlparse(url).hostname or "")
    host = (urlparse(str(data["url"])).hostname or "").lower()
    if not want:
        return False
    return not (host == want or host.endswith("." + want))


def _page_meta(data: dict) -> dict:
    """What the page said about itself beyond the rows: eBay's own match count, how many
    padded "fewer words" rows were left out, and whether it rewrote the query."""
    head = data.get("headline_count")
    return {
        "headline_count": int(head) if isinstance(head, (int, float)) else None,
        "related_rows_excluded": int(data.get("related_rows_excluded") or 0),
        "query_rewritten": bool(data.get("query_rewritten")),
    }


def _page_notes(meta: dict, found: int) -> list[str]:
    """Plain-language qualifiers the model must repeat. A padded page used to come back as
    "54 sold comps, median $31" for a query eBay itself matched to nothing."""
    notes: list[str] = []
    related = int(meta.get("related_rows_excluded") or 0)
    head = meta.get("headline_count")
    divider = "below eBay's 'Results matching fewer words' divider"
    if found == 0 and (related or head == 0):
        notes.append(
            f"eBay found no listings matching all the words in this query (its own count: {head}); "
            f"{related} loosely related listings {divider} were excluded. Broaden the query — do not "
            "price from those."
        )
    elif related and found < 5:
        notes.append(
            f"only {found} listings matched the whole query; {related} loosely related listings {divider} "
            "were excluded. A thin result — broaden the query before quoting a number."
        )
    elif related:
        notes.append(f"{related} loosely related listings {divider} were excluded from the statistics.")
    if meta.get("query_rewritten"):
        notes.append(
            "eBay rewrote this search ('Showing results for …'); every result is for ITS query, not "
            "yours — check the page before trusting these numbers."
        )
    return notes


def _with_meta(payload: dict, meta: dict, found: int) -> dict:
    payload["headline_count"] = meta.get("headline_count")
    payload["related_rows_excluded"] = meta.get("related_rows_excluded", 0)
    if meta.get("query_rewritten"):
        payload["query_rewritten"] = True
    notes = _page_notes(meta, found)
    if notes:
        payload["notes"] = notes
    return payload


def _as_bool(value, default: bool) -> bool:
    """A YAML/console flag: real bools pass through; the strings "false"/"no"/"0"/"off" mean False."""
    if value is None:
        return default
    if isinstance(value, str):
        text = value.strip().lower()
        if not text:  # a blank form field is "unset", not "off" — blank `headed` must not mean headless
            return default
        return text not in {"false", "no", "0", "off"}
    return bool(value)


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
        headed=_as_bool(cfg.get("headed"), True),
        stealth=_as_bool(cfg.get("stealth"), False),
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
            listings, dropped, meta = _fetch(b, url, sold=sold)
        except (BrowserError, EbayError) as exc:
            return json.dumps({"ok": False, "error": str(exc)})
        except ValueError as exc:
            return json.dumps({"ok": False, "error": str(exc)})

        cap = limit if limit and limit > 0 else max_results
        listings = listings[:cap]
        _record(query, "ebay", listings)
        stats = summarize(listings)
        return json.dumps(
            _with_meta(
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
                },
                meta,
                len(listings),
            )
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
            listings, dropped, meta = _fetch(b, url, sold=sold)
        except (BrowserError, EbayError, ValueError) as exc:
            return json.dumps({"ok": False, "error": str(exc)})
        return json.dumps(
            _with_meta(
                {
                    "ok": True,
                    "query": query,
                    "sold": sold,
                    "url": url,
                    "count": len(listings),
                    "unparseable_rows_skipped": dropped,
                    "listings": [x.as_dict() for x in listings[:max_results]],
                },
                meta,
                len(listings),
            )
        )

    @tool
    def ebay_session_status() -> str:
        """Is the browser signed in to eBay? Check this first when a tool reports a sign-in wall.

        Opens eBay in the browser window and reports whether the session is signed in. If it
        isn't, sign in by hand in that window once — the profile persists it, so this is a
        one-time step.
        """
        b = _browser()
        home = f"https://{domain}"
        try:
            data = _read_page(b, home, SESSION_JS, settle=False)
        except (BrowserError, EbayError) as exc:
            return json.dumps({"ok": False, "error": str(exc)})
        if _hijacked(data, home):
            # Another tab answered twice. Saying "not signed in" here would send the operator to
            # sign in again for nothing — say what actually happened instead.
            return json.dumps(
                {
                    "ok": False,
                    "error": f"could not check: the browser kept answering from {data.get('url')} instead of "
                    f"{domain} (Chrome's Gemini side panel or another tab took the active slot). Close that "
                    "panel or tab in the browser window and try again.",
                }
            )
        signed_in = bool(data.get("signed_in"))
        next_step = ""
        if not signed_in:
            next_step = "Sign in to eBay in the browser window that just opened; the profile keeps you signed in."
            if not b.stealth:
                # Chrome under CDP control carries navigator.webdriver, and Google's sign-in page
                # refuses such a browser. That is a config fix, not something the operator can click past.
                next_step += (
                    " If Google refuses the sign-in as an insecure browser, set ebay.stealth: true; "
                    "once the config has reloaded, the browser relaunches with it on the next call."
                )
        return json.dumps(
            {
                "ok": True,
                "signed_in": signed_in,
                "greeting": (data or {}).get("greeting", ""),
                "next_step": next_step,
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
            data = _read_page(b, url, RESULT_JS, settle=False)
        except (BrowserError, EbayError) as exc:
            return json.dumps({"ok": False, "error": str(exc)})
        rows = (data or {}).get("rows") or []
        return json.dumps(
            {
                "ok": True,
                "url": url,
                "landed_url": data.get("url"),
                "not_the_requested_site": _hijacked(data, url),
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
            listings, dropped, meta = _fetch(b, url, sold=True)
        except (BrowserError, EbayError, ValueError) as exc:
            return json.dumps({"ok": False, "error": str(exc)})

        listings = listings[:max_results]
        stats = summarize(listings)
        if not stats.get("count"):
            return json.dumps(
                _with_meta(
                    {"ok": True, "query": query, "stats": stats, "note": "no sold comps found — try a broader query"},
                    meta,
                    0,
                )
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
            _with_meta(
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
                },
                meta,
                len(listings),
            )
        )

    amazon_domain = cfg.get("amazon_domain") or "www.amazon.com"

    def _history():
        """The observation log, or None if it can't be opened — history is a nice-to-have and
        must never take a price check down with it."""
        try:
            from .history import PriceHistory

            return PriceHistory(cfg.get("history_db") or _default_history_db())
        except Exception:  # noqa: BLE001
            log.exception("[ebay] price history unavailable")
            return None

    def _record(key: str, source: str, listings) -> None:
        if (h := _history()) is not None:
            try:
                h.record(key, source, listings)
            except Exception:  # noqa: BLE001
                log.exception("[ebay] recording price history failed")

    @tool
    def amazon_price_check(query: str, sort: str = "relevance", limit: int = 0) -> str:
        """What is this item selling for on Amazon right now? Returns median/quartile statistics plus a sample of listings.

        These are ASKING prices — Amazon publishes no sold history, so nothing here says what
        buyers actually paid. Use eBay's sold comps for that. Sponsored placements are flagged
        and excluded from the statistics: an ad is what a seller paid to be shown, not what
        the market charges.
        """
        b = _browser()
        try:
            url = amazon.search_url(query, domain=amazon_domain, sort=sort)
            listings, dropped = _fetch_amazon(b, url)
        except (BrowserError, EbayError, ValueError) as exc:
            return json.dumps({"ok": False, "error": str(exc)})

        cap = limit if limit and limit > 0 else max_results
        listings = listings[:cap]
        _record(query, "amazon", listings)
        return json.dumps(
            {
                "ok": True,
                "query": query,
                "source": "amazon",
                "basis": "active Amazon listings — asking prices (Buy Box), NOT sale prices",
                "url": url,
                "stats": summarize(listings),
                "results_found": len(listings),
                "unparseable_rows_skipped": dropped,
                "sample": [x.as_dict() for x in listings[:_SAMPLE]],
            }
        )

    @tool
    def price_history(query: str, days: int = 90, source: str = "", check_now: bool = True) -> str:
        """Where today's price sits against what we've recorded before. Use for "is this cheap right now?".

        IMPORTANT — this history is SELF-RECORDED. Amazon publishes no price history, and the
        services that reconstruct one are paid third parties, so this plugin logs what it sees
        on every price check and reads that back. There is no data from before you enabled it,
        and a gap in the record means nobody searched, not that the price held steady. Say so
        rather than implying a continuous series.

        source: "amazon", "ebay", or blank for both. check_now=True takes a fresh Amazon
        reading first so the comparison includes today.
        """
        h = _history()
        if h is None:
            return json.dumps({"ok": False, "error": "the price-history store could not be opened"})

        current = None
        fresh_error = ""
        if check_now:
            try:
                listings, _ = _fetch_amazon(_browser(), amazon.search_url(query, domain=amazon_domain))
                _record(query, "amazon", listings)
                if stats := summarize(listings):
                    current = stats.get("median")
            except (BrowserError, EbayError, ValueError) as exc:
                # Reported, not swallowed: a history summary that silently excludes today
                # would answer "is this cheap NOW?" without today's price in it.
                fresh_error = str(exc)

        from .history import position

        hist = h.summary(query, days=days, source=source or "")
        if current is None and hist.get("observations"):
            current = hist.get("median")
        return json.dumps(
            {
                "ok": True,
                "query": query,
                "source": source or "all",
                "history": hist,
                "position": position(current, hist),
                "caveat": (
                    "Self-recorded history: nothing before this plugin was enabled, and gaps mean "
                    "nobody searched, not that the price was stable."
                ),
                **({"fresh_reading_failed": fresh_error} if fresh_error else {}),
            }
        )

    @tool
    def compare_prices(query: str, condition: str = "any") -> str:
        """Compare what an item goes for across eBay and Amazon in one call. Use when asked where something is cheapest, or what the market looks like overall.

        Returns eBay SOLD comps (what buyers paid), eBay ACTIVE and Amazon ACTIVE (what
        sellers are asking) side by side, each labelled with its basis. These are different
        kinds of number and are never averaged together — the sold figure is the one to price
        against; the asking figures say what a new listing would sit beside.

        A source that fails reports its reason instead of silently dropping out, so a
        one-sided comparison can never be mistaken for a complete one.
        """
        b = _browser()
        out: dict = {"ok": True, "query": query, "sources": {}}

        def _run(key, basis, fetch):
            try:
                fetched = fetch()
                listings, dropped = fetched[0], fetched[1]
                meta = fetched[2] if len(fetched) > 2 else {}  # the Amazon reader has no padding meta
                # Record here too. Only the single-source checks logged at first, so the
                # comparison — the tool most likely to be run repeatedly on a watched item —
                # built no history at all.
                _record(query, "amazon" if key.startswith("amazon") else "ebay", listings)
                out["sources"][key] = _with_meta(
                    {
                        "ok": True,
                        "basis": basis,
                        "stats": summarize(listings),
                        "results_found": len(listings),
                        "unparseable_rows_skipped": dropped,
                        "sample": [x.as_dict() for x in listings[:3]],
                    },
                    meta,
                    len(listings),
                )
            except (BrowserError, EbayError, ValueError) as exc:
                # Named, not omitted: a missing source that looks like an absent one turns a
                # half-answer into a confident whole one.
                out["sources"][key] = {"ok": False, "error": str(exc), "basis": basis}

        _run(
            "ebay_sold",
            "eBay sold listings — what buyers actually paid",
            lambda: _fetch(b, search_url(query, domain=domain, sold=True, condition=condition), sold=True),
        )
        _run(
            "ebay_active",
            "eBay active listings — asking prices",
            lambda: _fetch(b, search_url(query, domain=domain, sold=False, condition=condition), sold=False),
        )
        _run(
            "amazon_active",
            "Amazon active listings — asking prices (Buy Box)",
            lambda: _fetch_amazon(b, amazon.search_url(query, domain=amazon_domain)),
        )

        ok_sources = [k for k, v in out["sources"].items() if v.get("ok")]
        out["note"] = (
            "Sold and asking prices are different measures and are deliberately not combined. "
            "Price against the sold figure; read the asking figures as the competition."
        )
        if not ok_sources:
            out["ok"] = False
            out["error"] = "every source failed — see sources for the individual reasons"
        elif len(ok_sources) < 3:
            out["partial"] = f"only {len(ok_sources)} of 3 sources returned data"
        return json.dumps(out)

    return [
        ebay_price_check,
        ebay_price_and_profit,
        ebay_net_proceeds,
        ebay_breakeven,
        ebay_search,
        ebay_session_status,
        ebay_page_probe,
        amazon_price_check,
        compare_prices,
        price_history,
    ]

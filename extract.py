"""eBay URL construction + result extraction. No I/O — every function here is pure, so the
number-handling can be tested exhaustively without a browser.

Two rules shape this module, both of them about not inventing numbers:

1. **Parse, never guess.** A price string that doesn't parse becomes ``None``, not ``0.0``.
   A row with no usable price is dropped and counted, not smuggled through at zero — a
   single phantom $0 comp drags an average down and reads as a real data point.
2. **Empty and broken must not look alike.** "This search genuinely has no results" and
   "eBay changed its markup and the selector matched nothing" are the same empty list to a
   caller, and the second one silently becomes "nothing sells at that price". The page
   script reports which it saw (:data:`RESULT_JS` returns ``found_container``), and callers
   raise on the second.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from urllib.parse import urlencode

#: eBay sort codes (``_sop``) worth exposing. Best Match is eBay's default relevance sort.
SORTS = {
    "best_match": "12",
    "price_low": "15",  # price + shipping, lowest first
    "price_high": "16",  # price + shipping, highest first
    "newest": "10",
    "ending_soonest": "1",
}

#: Condition filter ids (``LH_ItemCondition``).
CONDITIONS = {"new": "3", "used": "4", "any": ""}


def search_url(
    query: str,
    *,
    domain: str = "www.ebay.com",
    sold: bool = False,
    sort: str = "best_match",
    condition: str = "any",
    max_price: float | None = None,
    min_price: float | None = None,
) -> str:
    """Build an eBay search URL.

    ``sold=True`` adds ``LH_Sold``/``LH_Complete``, which is the *sold comps* view — what
    items actually changed hands for. That's the number a seller prices against, and it's
    the same data the Marketplace Insights API gates behind a closed Limited Release
    programme; it is plainly visible in the normal web UI to any signed-in user.
    """
    if not (query or "").strip():
        raise ValueError("query is required")
    params: dict[str, str] = {"_nkw": query.strip()}
    if sold:
        params["LH_Sold"] = "1"
        params["LH_Complete"] = "1"
    if sort_code := SORTS.get(sort):
        params["_sop"] = sort_code
    if cond := CONDITIONS.get(condition, ""):
        params["LH_ItemCondition"] = cond
    if min_price is not None:
        params["_udlo"] = f"{min_price:g}"
    if max_price is not None:
        params["_udhi"] = f"{max_price:g}"
    return f"https://{domain}/sch/i.html?{urlencode(params)}"


# ── money ───────────────────────────────────────────────────────────────────────────
#: "$24.99", "C $1,299.00", "£12.50", "EUR 9,99" — symbol/code, then the number.
_MONEY = re.compile(r"(?P<cur>[A-Z]{0,3}\s*[$£€¥]|[A-Z]{3})?\s*(?P<num>[\d.,]+)")


def parse_money(text: str) -> tuple[float | None, float | None, str]:
    """``(low, high, currency)`` from an eBay price cell.

    Ranges ("$24.99 to $39.99") keep both ends — collapsing a range to its low end
    understates the market and collapsing to the mean invents a price nobody listed.
    Returns ``(None, None, "")`` when nothing parses, which callers must treat as
    "unknown", never as zero.
    """
    s = (text or "").strip()
    if not s:
        return None, None, ""
    currency = ""
    values: list[float] = []
    for m in _MONEY.finditer(s):
        raw = m.group("num")
        if not any(c.isdigit() for c in raw):
            continue
        if not currency and (cur := (m.group("cur") or "").strip()):
            currency = cur
        val = _to_float(raw)
        if val is not None:
            values.append(val)
    if not values:
        return None, None, currency
    return (values[0], values[-1] if len(values) > 1 else None, currency)


def _to_float(raw: str) -> float | None:
    """``"1,299.00"`` and ``"1.299,00"`` both mean the same thing in different locales.

    Decide by which separator comes LAST — that one is the decimal point. Guessing wrong
    turns €1.299,00 into €1.30, which is exactly the kind of confidently-wrong number this
    module exists to prevent.
    """
    s = raw.strip().rstrip(".,")
    if not s:
        return None
    last_dot, last_comma = s.rfind("."), s.rfind(",")
    if last_dot >= 0 and last_comma >= 0:
        # 1.299,00 → comma is the decimal; 1,299.00 → dot is. Whichever comes LAST wins.
        s = s.replace(".", "").replace(",", ".") if last_comma > last_dot else s.replace(",", "")
    elif last_comma >= 0:
        # A lone comma is a decimal point only if it isn't a thousands group (1,299).
        s = s.replace(",", "." if len(s) - last_comma - 1 != 3 else "")
    return _safe_float(s)


def _safe_float(s: str) -> float | None:
    try:
        return float(s)
    except ValueError:
        return None


# ── rows ────────────────────────────────────────────────────────────────────────────
@dataclass
class Listing:
    title: str
    url: str
    price: float | None
    price_high: float | None
    currency: str
    shipping: float | None
    condition: str
    sold_date: str
    #: True only for rows pulled from the sold/completed view. Callers surface this so an
    #: asking price is never reported as a sale price. Always False off eBay — no other
    #: marketplace here publishes what things actually sold for.
    sold: bool
    #: Which marketplace this row came from. Rows from different sources routinely sit in one
    #: list, and a price is only interpretable next to where it was quoted.
    source: str = "ebay"
    rating: float | None = None
    #: Paid placement. Excluded from statistics — an ad is what a seller paid to show you,
    #: not what the market is charging, and letting ads into a median biases it upward.
    sponsored: bool = False

    def as_dict(self) -> dict:
        return asdict(self)


#: Chrome that eBay bakes into the visible title text. Verified live: every anchor carries a
#: screen-reader "Opens in a new window or tab", and fresh listings are prefixed "New Listing".
#: Left in place these leak into titles the agent shows and compares.
_TITLE_NOISE = re.compile(r"\s*Opens in a new window or tab\s*$|^\s*New Listing\s*", re.IGNORECASE)


def clean_title(text: str) -> str:
    return _TITLE_NOISE.sub("", (text or "").strip()).strip()


def normalize(raw_rows: list[dict], *, sold: bool) -> tuple[list[Listing], int]:
    """Turn the page script's raw strings into typed rows.

    Returns ``(listings, dropped)``. ``dropped`` counts rows with no usable price — they are
    excluded rather than zero-filled, and reported so a caller can say "12 of 60 results had
    no readable price" instead of quietly averaging over a hole.
    """
    out: list[Listing] = []
    dropped = 0
    for r in raw_rows or []:
        title = clean_title(r.get("title") or "")
        url = (r.get("url") or "").strip()
        low, high, currency = parse_money(r.get("price") or "")
        if not title or low is None:
            dropped += 1
            continue
        ship_low, _, _ = parse_money(r.get("shipping") or "")
        if ship_low is None and _is_free_shipping(r.get("shipping") or ""):
            ship_low = 0.0
        out.append(
            Listing(
                title=title,
                url=url,
                price=low,
                price_high=high,
                currency=currency,
                shipping=ship_low,
                condition=(r.get("condition") or "").strip(),
                sold_date=(r.get("sold_date") or "").strip(),
                sold=sold,
            )
        )
    return out, dropped


def _is_free_shipping(text: str) -> bool:
    return "free" in (text or "").lower()


def summarize(listings: list[Listing]) -> dict:
    """Descriptive stats a pricing decision actually rests on.

    Median, not mean: eBay result sets routinely carry a lot-of-50 listing or a mispriced
    outlier next to single units, and a mean chases them. Both are returned so the gap
    between them is visible — a wide one is itself the signal that the query needs
    narrowing.
    """
    # Sponsored rows are paid placement — what a seller paid to put in front of you, not what
    # the market is charging. Amazon salts several into every results page, and letting them
    # into a median biases it upward. Excluded, and counted so the exclusion is visible.
    priced = [x for x in listings if x.price is not None]
    ranked = [x for x in priced if not x.sponsored]
    excluded = len(priced) - len(ranked)
    totals = sorted(x.price + (x.shipping or 0.0) for x in ranked)
    if not totals:
        return {"count": 0, "sponsored_excluded": excluded} if excluded else {"count": 0}
    n = len(totals)
    mid = n // 2
    median = totals[mid] if n % 2 else (totals[mid - 1] + totals[mid]) / 2
    stats = {
        "count": n,
        "median": round(median, 2),
        "mean": round(sum(totals) / n, 2),
        "low": round(totals[0], 2),
        "high": round(totals[-1], 2),
        # Quartiles bound "what it usually goes for" without the tails.
        "p25": round(totals[max(0, int(n * 0.25) - (1 if n % 4 == 0 else 0))], 2),
        "p75": round(totals[min(n - 1, int(n * 0.75))], 2),
        "basis": "item price + shipping, where shipping was stated",
    }
    if excluded:
        stats["sponsored_excluded"] = excluded
    return stats


# ── the page script ─────────────────────────────────────────────────────────────────
#: Extract search-result rows from the DOM.
#:
#: Selector lists are ordered fallbacks, because eBay's result markup is not a contract and
#: changes without notice. The script reports ``found_container`` so the caller can tell a
#: genuinely empty result set from markup that moved — the distinction that keeps "no comps
#: found" from silently meaning "the scraper broke".
RESULT_JS = r"""
(() => {
  // eBay is mid-migration between two result layouts and serves both: the older
  // `s-item` (dedicated .s-item__shipping etc.) and the newer `s-card`, where every
  // fact — price, delivery, location, seller — is an undifferentiated
  // `.s-card__attribute-row` and only its TEXT says which is which. Verified live:
  //   ["$284.99", "$262.19 with coupon", "Buy It Now", "+$12.99 delivery",
  //    "Located in United States", "39+ watchers", "seller 99.9% positive (40.1K)"]
  // So new-layout fields are classified by content, not position.
  const txt = (el) => (el && el.textContent ? el.textContent.trim() : "");
  const pick = (root, sels) => {
    for (const s of sels) { const t = txt(root.querySelector(s)); if (t) return t; }
    return "";
  };
  const CARD = ["li.s-item", "li.s-card", "ul.srp-results > li[data-viewport]", "[data-testid='item-card']"];
  let cards = [];
  for (const sel of CARD) { const f = document.querySelectorAll(sel); if (f.length) { cards = [...f]; break; } }
  const container = document.querySelector("ul.srp-results, .srp-river-results, .srp-results__list, [data-testid='search-results']");

  // A search with few (or no) exact matches is PADDED: eBay renders the exact matches, then
  // a divider reading "Results matching fewer words", then dozens of loosely related items
  // (other teams' dice, complete boxes, $1 transfer sheets). Verified live 2026-09-12 on a
  // query whose headline said "0 results": 2 filler cards, the divider, then 54 cards —
  // which this script used to report as 54 sold comps with a median. Everything below the
  // divider is excluded from `rows` and counted in `related_rows_excluded` instead.
  const leafText = (el) => (el.children.length === 0 ? (el.textContent || "").trim() : "");
  const divider = [...document.querySelectorAll("li, div, h2, h3, h4, span, p")].find((el) =>
    /^results matching fewer words$|^results? for similar (searches|items)$/i.test(leafText(el))
  ) || null;
  const afterDivider = (c) => !!divider && !!(divider.compareDocumentPosition(c) & Node.DOCUMENT_POSITION_FOLLOWING);
  // eBay's own count of exact matches ("1,234 results for …", or "0 results for …" on a
  // padded page). Read so the caller can tell "few comps" from "none, and the rest is filler".
  const headText = pick(document, [".srp-controls__count-heading", "h1.srp-controls__count-heading",
                                   ".srp-controls__count", "[data-testid='count-heading']"]) ||
                   (document.body ? document.body.innerText.slice(0, 3000) : "");
  const headMatch = headText.match(/([\d,]+)\+?\s*results?\b/i);
  const headline_count = headMatch ? parseInt(headMatch[1].replace(/,/g, ""), 10) : null;
  // "Showing results for X — Search instead for Y": eBay rewrote the query, so EVERY card is
  // for its query, not the caller's.
  const controlsText = pick(document, [".srp-controls", ".srp-rewrite", ".srp-save-null-search"]) ||
                       (document.body ? document.body.innerText.slice(0, 3000) : "");
  const query_rewritten = /search instead for|showing results for/i.test(controlsText);

  const MONEYISH = /[$£€¥]\s*[\d.,]+/;
  const SHIPPING = /(delivery|shipping|postage|freight)/i;
  const rows = [];
  let related_rows_excluded = 0;
  for (const c of cards) {
    const title = pick(c, [".s-item__title", ".s-card__title", "[role='heading']", "h3"]);
    if (!title || /^shop on ebay$/i.test(title)) continue;  // eBay's own filler card
    if (afterDivider(c)) { related_rows_excluded += 1; continue; }
    const a = c.querySelector("a.s-item__link, a.s-card__link, a[href*='/itm/']");

    let price = pick(c, [".s-item__price", ".s-card__price"]);
    let shipping = pick(c, [".s-item__shipping", ".s-item__logisticsCost"]);
    const attrs = [...c.querySelectorAll(".s-card__attribute-row")].map(txt).filter(Boolean);
    if (!price) {
      // The bare price row, not "$262.19 with coupon" — a conditional price isn't the ask.
      price = attrs.find((t) => MONEYISH.test(t) && !SHIPPING.test(t) && !/coupon|off\b|was\b/i.test(t)) || "";
    }
    if (!shipping) {
      shipping = attrs.find((t) => SHIPPING.test(t)) || "";
    }
    rows.push({
      title,
      url: a ? a.href.split("?")[0] : "",
      price,
      shipping,
      condition: pick(c, [".SECONDARY_INFO", ".s-item__subtitle", ".s-card__subtitle"]),
      // Sold rows carry a "Sold <date>" caption; on the active view this is simply absent.
      sold_date: (attrs.find((t) => /^sold\b/i.test(t)) ||
                  pick(c, [".s-item__caption--signal", ".s-item__title--tagblock", ".s-card__caption"]) || ""),
      attributes: attrs.slice(0, 10),
    });
  }
  return JSON.stringify({
    // Reported so a failure can name where the browser ACTUALLY ended up — eBay bounces
    // through a redirect chain, and "couldn't read the page" is unfalsifiable without it.
    url: location.href,
    title: (document.title || ""),
    found_container: !!container || cards.length > 0,
    // eBay interposes an interstitial ("Pardon Our Interruption", /splashui/challenge).
    // Its DOM has no cards, which is indistinguishable from "nothing matched" unless we
    // say so here — and "no comps found" is a far more damaging wrong answer than an error.
    // eBay gates behind SEVERAL interstitials, all of which render a card-less page:
    // /splashui/challenge ("Pardon Our Interruption") and /splashui/captcha ("Security
    // Measure | eBay" — "Please verify yourself to continue"). Matching only the first, as
    // this did originally, reported a live CAPTCHA as "markup changed, file a bug" — wrong
    // diagnosis, and it sends the operator looking for a defect instead of at the browser
    // window where a human check is waiting. Match the /splashui/ prefix, not one page.
    challenge: /\/splashui\/|pardon our interruption|security measure|verify yourself/i.test(
      location.href + " " + (document.title || "")
    ),
    signin_wall: /signin\.ebay/.test(location.href + " " + (document.referrer || "")),
    count: rows.length,
    headline_count,
    related_rows_excluded,
    query_rewritten,
    rows,
  });
})()
"""

#: Is this browser session signed in to eBay? Every seller-side page silently redirects to a
#: sign-in wall otherwise, and a scraper that can't tell the difference reports "you have no
#: listings" to someone with two hundred.
SESSION_JS = r"""
(() => {
  const t = document.body ? document.body.innerText : "";
  const signedOut = /sign in|signin/i.test(t) && !!document.querySelector("a[href*='signin.ebay']");
  const greet = document.querySelector("#gh-ug, .gh-identity__greeting, [data-testid='greeting']");
  return JSON.stringify({
    url: location.href,
    signed_in: !!greet || (!signedOut && /my ebay|seller hub/i.test(t)),
    on_signin_page: /signin\.ebay/.test(location.href),
    greeting: greet ? greet.textContent.trim().slice(0, 80) : "",
  });
})()
"""

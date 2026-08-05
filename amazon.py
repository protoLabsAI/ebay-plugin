"""Amazon as a second price source, read the same way eBay is: a real browser.

There is no API path worth taking. Amazon's Product Advertising API 5.0 is **deprecated and
returns HTTP 403** (per Amazon's own migration notice), and its replacement — the Creators
API — requires an Amazon Associates account carrying qualifying referred sales. Neither is
available to a seller who just wants to know what a thing costs.

**Amazon is not a comp source in the way eBay is.** eBay publishes what items actually SOLD
for; Amazon publishes what is being asked today, and nothing else. Every row from here is
``sold=False``, and callers must never let an Amazon price stand in for a sale price.

Two Amazon-specific distortions this module corrects, both of which would otherwise skew a
median upward:

* **Sponsored placements are ads.** They are what a seller paid to show you, not what the
  market is charging. Flagged, and excluded from the statistics.
* **The price shown is the Buy Box.** One offer among several, chosen by Amazon. It's the
  right number for "what would a buyer pay today", and the wrong one for "what is the
  cheapest this can be had for".

Selectors verified live on 2026-08-05 against a real results page.
"""

from __future__ import annotations

import re
from urllib.parse import urlencode

from .extract import Listing, clean_title, parse_money

#: Amazon sort keys (``s``). Amazon's default relevance sort has no explicit value.
SORTS = {
    "relevance": "",
    "price_low": "price-asc-rank",
    "price_high": "price-desc-rank",
    "newest": "date-desc-rank",
    "rating": "review-rank",
}


def search_url(
    query: str,
    *,
    domain: str = "www.amazon.com",
    sort: str = "relevance",
    min_price: float | None = None,
    max_price: float | None = None,
) -> str:
    """Build an Amazon search URL.

    Price bounds use ``low-price``/``high-price``, which Amazon applies in whole currency
    units.
    """
    if not (query or "").strip():
        raise ValueError("query is required")
    params: dict[str, str] = {"k": query.strip()}
    if sort_key := SORTS.get(sort, ""):
        params["s"] = sort_key
    if min_price is not None:
        params["low-price"] = f"{min_price:g}"
    if max_price is not None:
        params["high-price"] = f"{max_price:g}"
    return f"https://{domain}/s?{urlencode(params)}"


#: Extract search-result rows from an Amazon results page.
#:
#: Verified live: cards are ``div[data-component-type="s-search-result"]`` carrying
#: ``data-asin``; the reliable price is ``.a-price .a-offscreen`` (already formatted, e.g.
#: "$379.00") rather than the split ``.a-price-whole``/``.a-price-fraction`` pair, which
#: renders as "379." and loses the cents. Title comes from ``h2`` — ``[data-cy=title-recipe]``
#: appends catalogue noise ("ESRB Rating: Everyone") that would pollute comparisons.
RESULT_JS = r"""
(() => {
  const txt = (el) => (el && el.textContent ? el.textContent.trim() : "");
  const CARD = ['div[data-component-type="s-search-result"]', '[data-asin]:not([data-asin=""])', ".s-result-item"];
  let cards = [];
  for (const s of CARD) { const f = document.querySelectorAll(s); if (f.length) { cards = [...f]; break; } }
  const container = document.querySelector('.s-main-slot, [data-component-type="s-search-results"]');

  const rows = [];
  for (const c of cards) {
    const title = txt(c.querySelector("h2"));
    if (!title) continue;
    const a = c.querySelector('a.a-link-normal[href*="/dp/"], a[href*="/dp/"]');
    // .a-offscreen is the screen-reader copy of the assembled price — the only node that
    // carries the WHOLE amount including cents and currency.
    const price = txt(c.querySelector(".a-price .a-offscreen"));
    const ratingText = txt(c.querySelector('[data-cy="reviews-ratings-slot"]')) || txt(c.querySelector(".a-icon-alt"));
    rows.push({
      title,
      asin: c.getAttribute("data-asin") || "",
      url: a ? a.href.split("?")[0] : "",
      price,
      delivery: txt(c.querySelector('[data-cy="delivery-recipe"]')),
      rating: ratingText,
      // Amazon labels paid placement in the card body; it is an ad, not a market price.
      sponsored: /sponsored/i.test(c.textContent.slice(0, 300)),
    });
  }
  return JSON.stringify({
    url: location.href,
    title: document.title || "",
    found_container: !!container || cards.length > 0,
    // Amazon's bot wall. Like eBay's, it renders a card-less page, which is
    // indistinguishable from "nothing matched" unless it's named here.
    challenge: /captcha|robot check|automated access|errors\/validateCaptcha/i.test(
      location.href + " " + (document.title || "") + " " + (document.body ? document.body.innerText.slice(0, 400) : "")
    ),
    signin_wall: /\/ap\/signin/.test(location.href),
    count: rows.length,
    rows,
  });
})()
"""

#: "4.7 out of 5 stars" → 4.7
_RATING = re.compile(r"([\d.]+)\s*out of")


def normalize(raw_rows: list[dict]) -> tuple[list[Listing], int]:
    """Turn Amazon's raw strings into the shared row type.

    Same contract as the eBay normalizer: a row with no usable price is dropped and counted,
    never zero-filled. ``sold`` is always ``False`` — Amazon does not publish sale prices.
    """
    out: list[Listing] = []
    dropped = 0
    for r in raw_rows or []:
        title = clean_title(r.get("title") or "")
        low, high, currency = parse_money(r.get("price") or "")
        if not title or low is None:
            dropped += 1
            continue
        rating = None
        if m := _RATING.search(r.get("rating") or ""):
            try:
                rating = float(m.group(1))
            except ValueError:
                rating = None
        delivery = r.get("delivery") or ""
        out.append(
            Listing(
                title=title,
                url=(r.get("url") or "").strip(),
                price=low,
                price_high=high,
                currency=currency,
                # Amazon quotes delivery as prose ("FREE delivery Fri, Aug 7"), not a line
                # item. Free is knowable; a paid figure usually isn't shown until checkout,
                # so it stays None (unknown) rather than being guessed at zero.
                shipping=0.0 if "free" in delivery.lower() else None,
                condition="",
                sold_date="",
                sold=False,
                source="amazon",
                rating=rating,
                sponsored=bool(r.get("sponsored")),
            )
        )
    return out, dropped

# ebay-plugin

Pricing research on eBay for [protoAgent](https://github.com/protoLabsAI/protoAgent) — **what an
item actually sold for**, and **what you'd keep after fees**.

```
ebay_price_and_profit("nintendo switch oled", item_cost=120, shipping_cost=12)

  stats        median $200.75   p25–p75 $170.49–$225.99   (60 sold comps)
  breakeven    $155.32
  at median    sale $200.75 → fees $27.70 → net $41.05 (20.4%)
```

## Why a browser and not the eBay API

The number a seller prices against is **sold** comps. That data lives behind eBay's
Marketplace Insights API, which eBay lists as **Limited Release and closed to new
applicants** — while the same data is plainly visible in the normal web UI to a signed-in
user. So this drives a real signed-in browser (the
[`agent-browser`](https://www.npmjs.com/package/agent-browser) CLI) instead of an API.

No developer keys, no OAuth, and the plugin never handles your credentials — you sign in by
hand, once, in a browser window.

**What that costs, stated plainly:**

| | |
|---|---|
| **Sign-in required** | eBay serves the sold view only to signed-in users. |
| **Headed only** | eBay answers headless browsers with an error page. |
| **Rate-limited** | Hit it hard and eBay serves verification pages or results-less pages for a while. `min_interval_s` paces requests; raise it if that happens. |
| **Markup isn't a contract** | Selectors were verified live and will eventually break. The tools distinguish "no results" from "couldn't read the page" and report the second as a bug. |

This plugin makes **no attempt to defeat bot detection** — no proxy rotation, no fingerprint
spoofing, no CAPTCHA solving. It drives a normal browser at a human pace. When eBay asks for
verification, the tools say so and hand it to you.

## Setup

```bash
npm i -g agent-browser && agent-browser install       # the browser CLI
protoagent plugin install https://github.com/protoLabsAI/ebay-plugin
```

```yaml
# langgraph-config.yaml
plugins:
  enabled: [ebay]
ebay:
  profile: ~/.protoagent/ebay-profile   # REQUIRED — this is what keeps you signed in
  headed: true
  domain: www.ebay.com                  # www.ebay.co.uk, www.ebay.de, …
```

Then ask the agent to run `ebay_session_status`, and **sign in once** in the window that
opens. The profile persists it.

> `profile` and `headed` are **daemon-level** launch options in `agent-browser`. If a browser
> daemon is already running under different options the CLI ignores them silently — so the
> plugin raises instead of browsing as the wrong identity. `agent-browser close --all`, then
> retry.

## Tools

| tool | what it's for |
|---|---|
| `ebay_price_and_profit` | The one-call answer to *"what should I list this at?"* — sold comps run through fees and your costs. |
| `ebay_price_check` | Median/quartile stats for an item. Sold by default; `sold=False` for asking prices. |
| `ebay_net_proceeds` | Itemized fees, costs, net and margin for one sale. |
| `ebay_breakeven` | The lowest price that doesn't lose money. |
| `ebay_search` | Individual listings — competitor titles, what's bundled. |
| `ebay_session_status` | Signed in? Run this first when something reports a sign-in wall. |
| `ebay_page_probe` | Diagnostic: what the extractor sees on a page. For when markup changes. |

A bundled `ebay-pricing` skill teaches the agent how to narrow a query until the comps are
actually comparable, and how to report a price *with its basis*.

## Fees

Fee rates are **config, not knowledge**. Schedules change and vary by category, store
subscription and country, so the shipped defaults carry their own provenance and every result
says whether they've been verified:

```yaml
ebay:
  fees:
    final_value_pct: 13.6      # most US categories, no store
    per_order_fee: 0.40        # 0.30 at/under $10
    international_pct: 1.65
    promoted_pct: 0.0          # only if the sale came via a Promoted Listing
    verified_on: "2026-08-05"  # set this once you've checked a real invoice
```

Until `verified_on` is set, every result carries an `UNVERIFIED` caveat. Two subtleties the
calculator gets right and most don't:

- **Sales tax inflates the fee base.** eBay computes its cut on a total that includes tax you
  never receive.
- **Free shipping isn't free.** It moves the cost to you while eBay still charges on what the
  buyer paid.

Payment processing defaults to **zero** — eBay's managed payments bundles it into the final
value fee, so charging it again would double-count. A `PAYPAL_GOODS_SERVICES` schedule is
provided for sales taken *outside* eBay.

## What this does NOT do

- **No Amazon.** Amazon's Product Advertising API is deprecated and returns `403`; its
  replacement (Creators API) requires an Associates account with qualifying referred sales.
  There's no Amazon data here, and the skill tells the agent to say so rather than reason
  about Amazon from memory. The tool layer is source-agnostic, so a provider can be added
  without redesign.
- **No listing management.** Nothing here publishes a listing, changes a live price, contacts
  a buyer or touches an order. Seller-side automation is deliberately deferred until the
  Seller Hub markup can be verified against a signed-in session — guessing at it is precisely
  the mistake that produced three defects during this plugin's own development.
- **No sell-through rate.** Sold comps show what sold, never how many didn't.

## Development

```bash
pip install -r requirements-dev.txt
pytest -q          # 77 tests, no protoAgent host and no browser required
ruff check . && ruff format --check .
```

The money-handling fixtures are **real strings captured from a live eBay results page**, not
imagined ones. That matters: the first draft used `.s-item__shipping` from memory, which
matches nothing on eBay's current `s-card` layout — it would have silently reported every
listing as free-shipping and ranked a $10-plus-$15 item above a $20-shipped one.

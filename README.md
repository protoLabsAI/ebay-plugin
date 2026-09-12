# marketplace price plugin (eBay + Amazon)

Price research for [protoAgent](https://github.com/protoLabsAI/protoAgent) — **what an
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
| **Rate-limited** | Hit either site hard and you get verification pages. `min_interval_s` paces requests; raise it if that happens. |
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
  stealth: false                        # true if your eBay account signs in through Google (below)
  domain: www.ebay.com                  # www.ebay.co.uk, www.ebay.de, …
  amazon_domain: www.amazon.com
```

Then ask the agent to run `ebay_session_status`, and **sign in once** in the window that
opens. The profile persists it.

### Signing in through Google

Chrome sets `navigator.webdriver` when it is driven over CDP, and Google refuses to sign an
account in from such a browser ("This browser or app may not be secure"). If your eBay account
signs in through Google, set `stealth: true`: the browser launches with
`--disable-blink-features=AutomationControlled` — the same flag protoAgent's core browser plugin
uses for its stealth option — and nothing else changes. The browser still identifies as Chrome
and runs at a human pace; this is not an attempt to defeat eBay's own checks. An eBay password
or passkey login needs none of this.

> `profile`, `headed` and `stealth` are **daemon-level** launch options in `agent-browser`. The
> plugin sends them with every page open and the daemon reconciles them against the running
> browser: unchanged → reused; changed → Chrome relaunches with the new options on the same
> profile (a sign-in survives); daemon gone → one is respawned with them. So a fresh agent
> process, a subagent with its own tool set, or a config change all converge on the right
> browser with nothing to close by hand. (Through 0.2.0 the launch step was a URL-less `open`,
> which the CLI turns into a second, option-less launch — every window it opened was replaced
> within seconds by one on a throwaway profile. Fixed in 0.3.0.) If you ever do need to reset
> the browser: `agent-browser close --session ebay`, never `close --all`, which also kills
> every other plugin's browser.

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
| `amazon_price_check` | Amazon asking prices. Sponsored rows are flagged and kept out of the statistics. |
| `compare_prices` | eBay sold + eBay active + Amazon active, side by side, each labelled with its basis. |
| `price_history` | Where today's price sits against what we've recorded before. |

A bundled `ebay-pricing` skill teaches the agent how to narrow a query until the comps are
actually comparable, and how to report a price *with its basis*.

**Amazon is not a comp source.** It publishes asking prices only — the Buy Box, one offer
among several. eBay's sold view is the only real sale data here, and every result is
labelled so the two can never be reported as the same kind of number.

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

- **No Amazon SOLD data.** Amazon publishes asking prices only — nothing there says what
  buyers actually paid. eBay's sold comps are the only real sale data in this plugin, and the
  tools label which is which so the two can't be confused. (Amazon's Product Advertising API
  is deprecated and returns `403`; its replacement needs an Associates account with
  qualifying referred sales, so the browser is the only route.)
- **No retrospective price history.** `price_history` reads a log this plugin writes itself,
  so it starts empty and accumulates from the day you enable it. Gaps mean nobody searched,
  not that the price held steady. Reconstructing history before that needs a paid third party
  (Keepa and similar).
- **No listing management.** Nothing here publishes a listing, changes a live price, contacts
  a buyer or touches an order. Seller-side automation is deliberately deferred until the
  Seller Hub markup can be verified against a signed-in session — guessing at it is precisely
  the mistake that produced three defects during this plugin's own development.
- **No sell-through rate.** Sold comps show what sold, never how many didn't.

## Development

```bash
pip install -r requirements-dev.txt
pytest -q          # 114 tests, no protoAgent host and no browser required
ruff check . && ruff format --check .
```

The money-handling fixtures are **real strings captured from a live eBay results page**, not
imagined ones. That matters: the first draft used `.s-item__shipping` from memory, which
matches nothing on eBay's current `s-card` layout — it would have silently reported every
listing as free-shipping and ranked a $10-plus-$15 item above a $20-shipped one.

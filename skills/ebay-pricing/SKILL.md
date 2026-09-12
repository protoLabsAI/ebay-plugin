---
name: ebay-pricing
description: Price an item against real eBay data — sold comps, how to narrow a query until the comps are actually comparable, and how to report a price with its basis. Use when asked what something is worth, what to list at, or whether a price is right.
---

# Pricing against eBay

## The one rule

**Sold ≠ active.** Sold listings are what buyers paid. Active listings are what sellers
*hope* to get, and include items that will never sell at that price. Price against sold
comps; use active only to see what a new listing will sit next to.

`ebay_price_check` defaults to `sold=True`. Every result carries a `basis` field — repeat it
when you report a number. "Median $200 across 60 sold listings" and "median $200 across 60
current asking prices" are different claims and must never be stated the same way.

## The loop

1. **Search the item as a buyer would type it.** Include the details that change the price
   — model number, capacity, edition, bundle — and nothing that doesn't.
2. **Read `results_found`, `headline_count` and `notes` before the statistics.** Under ~10
   comps is a weak signal; say so rather than quoting a median to the cent off five data
   points. A non-empty `notes` list is part of the answer, not a footnote.
3. **Sanity-check the spread.** If `high` is many times `low`, or `p25` and `p75` are far
   apart, the query is catching different things — a lot of 10 next to a single unit, or a
   different model. Narrow it and search again. A tight p25–p75 band is what makes a
   recommendation trustworthy.
4. **Quote the band, not a point.** `p25`–`p75` is "what it usually goes for". A single
   median implies a precision the data doesn't have.
5. **Then apply the operator's economics.** Fees, shipping, materials, time. Comps say what
   the market pays; only the operator's numbers say whether that's worth doing.

## Reading the numbers

- `median` over `mean` — one mispriced lot drags a mean and the median shrugs it off. Both
  are returned; a big gap between them is itself a signal the result set is contaminated.
- Prices include stated shipping (`basis` says so). A $10 item with $15 shipping loses to a
  $20 item shipped free, and comparing item prices alone gets that backwards.
- `unparseable_rows_skipped` is rows with no readable price. A handful is normal. A large
  number next to a small `results_found` means treat the statistics as provisional.
- **`headline_count` and `related_rows_excluded`.** eBay pads a search with few exact matches:
  it shows the matches, then a divider reading "Results matching fewer words", then dozens of
  loosely related items. Only the exact matches are counted in `results_found` and the
  statistics; the padding is reported as `related_rows_excluded`. `headline_count` is eBay's
  own count of exact matches. `results_found: 0` with `related_rows_excluded: 54` means eBay
  found NOTHING for that query — broaden it; never price from the excluded rows.
- **`query_rewritten: true`** means eBay silently searched for something else ("Showing results
  for …"). Every number on that page is for its query, not yours. Say so and re-query.
- **The same quartiles showing up for different items** (five items all at p25 $20.22) is the
  signature of padding or a rewrite, not a market fact. Stop and look at the page.
- Repeat every entry in `notes` when you report a number. They exist because a padded page once
  came back as "54 sold comps, median $31" for a query eBay matched to nothing.

## When a tool returns an error

The tools distinguish "no results" from "couldn't read the page" deliberately. Never
paper over the second as "I didn't find anything" — that turns a plugin bug into a false
market claim.

- **sign-in wall** → run `ebay_session_status`, tell the operator to sign in once in the
  browser window. It persists after that. If the operator reports Google refusing the sign-in
  as an insecure browser, that is config, not something to click past: `ebay.stealth: true`,
  then `ebay_session_status` again once the config has reloaded — the browser relaunches
  with it.
- **challenge page** → eBay wants a human interaction; ask the operator to complete it. If
  it recurs, suggest raising `ebay.min_interval_s`.
- **couldn't find the results list** → eBay changed its markup. Run `ebay_page_probe` on the
  URL and report what it shows. This is a bug to file, not a market finding.

## What this cannot tell you

- **Sell-through rate.** Sold comps show what sold, never how many *didn't*. A high median
  on a slow-moving item is not a good price.
- **Amazon sold prices or history.** `amazon_price_check` returns what Amazon is *asking*
  right now (Buy Box, sponsored rows excluded) — Amazon publishes no sold data and no price
  history, and `price_history` holds only what this plugin has recorded since it was enabled.
  Use the Amazon number as a ceiling a new buyer compares against, never as a comp, and say
  which it is rather than reasoning about Amazon from memory.
- **Anything older than eBay shows.** The sold view reaches back a limited window.

Say which of these applies when it bears on the answer. A caveat stated is worth more than a
confident number that quietly rests on missing data.

## Net, not gross

A median sale price is not money in your pocket. eBay's cut is real and non-trivial, so any
recommendation that stops at the headline price is misleading.

- `ebay_price_and_profit` does the whole thing in one call: comps → fees → your costs → net at
  the p25 / median / p75 price. Prefer it for "what should I list this at?".
- `ebay_net_proceeds` itemizes a single sale you already know the numbers for.
- `ebay_breakeven` is the floor. Quote it when the operator is deciding whether a sale is
  worth making at all.

Two things the arithmetic accounts for that are easy to get wrong by hand:

- **Sales tax raises the fee base without raising your revenue.** eBay charges on a total
  including tax it collects from the buyer and remits — you never see that money.
- **Free shipping isn't free.** Absorbing postage moves the cost to you while eBay still
  charges its percentage on what the buyer paid.

**Always repeat the `caveat` when it's non-empty.** Until the operator has confirmed the fee
schedule against a real eBay invoice, every margin is provisional — rates vary by category,
store subscription and country. State that once, plainly, rather than presenting a net figure
as settled fact.

"""Price history — where today's price sits against what we've seen before.

**This history is self-recorded, and that is a real limitation, stated up front.** Amazon
publishes no price history, and the services that reconstruct one (Keepa, CamelCamelCamel)
are paid APIs or third-party sites with their own terms. So instead of pretending to
retrospective data, every price check writes what it saw to a local store, and this reads it
back. Consequences worth saying out loud to an operator:

* **It starts empty.** There is no history before the day the plugin is enabled. A summary
  over two observations is not a trend, and :func:`summary` reports ``observations`` so the
  caller can qualify rather than assert.
* **It only knows what was searched.** Gaps in the record are gaps in attention, not evidence
  that a price held steady.

What it is good at is the question a seller actually asks — *is this cheap right now?* — once
a few weeks of checks have accumulated, and it costs nothing and depends on no one.

Keyed by ASIN where there is one (stable across title edits) and by a normalized query
otherwise.
"""

from __future__ import annotations

import re
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS observations (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    key       TEXT NOT NULL,      -- ASIN, or a normalized query
    source    TEXT NOT NULL,      -- ebay | amazon
    price     REAL NOT NULL,      -- item + shipping where stated
    title     TEXT,
    url       TEXT,
    seen_at   TEXT NOT NULL       -- ISO-8601 UTC
);
CREATE INDEX IF NOT EXISTS idx_obs_key ON observations(key, seen_at);
"""


def normalize_key(text: str) -> str:
    """Collapse a query to a stable lookup key.

    "Nintendo Switch OLED" and "nintendo  switch   oled" are the same search and must land on
    the same history; otherwise a rephrase silently starts a fresh, empty record and the
    agent reports "no history" for something it has watched for weeks.
    """
    return re.sub(r"\s+", " ", (text or "").strip().lower())


class PriceHistory:
    """A tiny local observation log. One row per listing per check."""

    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as db:
            db.executescript(_SCHEMA)
            db.commit()

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(str(self.path))
        db.row_factory = sqlite3.Row
        return db

    def record(self, key: str, source: str, listings, *, now: str | None = None) -> int:
        """Log every priced, non-sponsored listing. Returns how many rows landed.

        Ads are excluded here as well as from the statistics — recording them would bake a
        seller's ad spend into the historical baseline, so a later "cheapest we've seen"
        would be measured against a price nobody was ever charging.
        """
        stamp = now or datetime.now(UTC).isoformat()
        key = normalize_key(key)
        rows = [
            (key, source, float(x.price) + float(x.shipping or 0.0), x.title, x.url, stamp)
            for x in listings
            if x.price is not None and not x.sponsored
        ]
        if not rows:
            return 0
        with closing(self._connect()) as db:
            db.executemany(
                "INSERT INTO observations (key, source, price, title, url, seen_at) VALUES (?,?,?,?,?,?)",
                rows,
            )
            db.commit()
        return len(rows)

    def summary(self, key: str, *, days: int = 90, source: str = "") -> dict:
        """Where the record sits over the last ``days``.

        Returns ``{"observations": 0}`` when nothing has been logged — an explicitly empty
        answer, so a caller can say "no history yet" instead of treating silence as stability.
        """
        since = (datetime.now(UTC) - timedelta(days=max(days, 1))).isoformat()
        sql = "SELECT price, seen_at FROM observations WHERE key = ? AND seen_at >= ?"
        params: list = [normalize_key(key), since]
        if source:
            sql += " AND source = ?"
            params.append(source)
        with closing(self._connect()) as db:
            rows = db.execute(sql + " ORDER BY seen_at", params).fetchall()
        if not rows:
            return {"observations": 0, "window_days": days}
        prices = sorted(r["price"] for r in rows)
        n = len(prices)
        mid = n // 2
        return {
            "observations": n,
            "window_days": days,
            "first_seen": rows[0]["seen_at"],
            "last_seen": rows[-1]["seen_at"],
            "distinct_days": len({r["seen_at"][:10] for r in rows}),
            "low": round(prices[0], 2),
            "high": round(prices[-1], 2),
            "median": round(prices[mid] if n % 2 else (prices[mid - 1] + prices[mid]) / 2, 2),
        }


def position(current: float | None, hist: dict) -> dict:
    """Place ``current`` against the recorded range — the actual question being asked.

    ``percentile`` is the share of past observations at or below the current price, so 0 means
    "the cheapest we have ever recorded" and 100 "the dearest". Returned only when there is
    enough history to mean anything: a percentile off three observations is arithmetic
    theatre, and quoting it would lend a number unearned authority.
    """
    if current is None or not hist.get("observations"):
        return {"verdict": "no history yet — this check starts the record"}
    low, high = hist["low"], hist["high"]
    out: dict = {
        "current": round(current, 2),
        "vs_low": round(current - low, 2),
        "vs_median": round(current - hist["median"], 2),
    }
    if hist["observations"] < 10 or hist.get("distinct_days", 0) < 3:
        out["verdict"] = (
            f"only {hist['observations']} observation(s) across "
            f"{hist.get('distinct_days', 0)} day(s) — too thin to call a trend"
        )
        return out
    span = high - low
    out["percentile"] = round((current - low) / span * 100, 1) if span > 0 else 0.0
    if current <= low:
        out["verdict"] = "the lowest recorded in this window"
    elif current >= high:
        out["verdict"] = "the highest recorded in this window"
    elif current < hist["median"]:
        out["verdict"] = "below the recorded median — cheap relative to what we've seen"
    else:
        out["verdict"] = "above the recorded median — dear relative to what we've seen"
    return out

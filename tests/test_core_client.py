"""The core-client path (coreclient.py), typed failures, the comps cache, and the fallback's timeouts.

Host-free: the host's ``agent_browser.client`` factory is replaced by a fake whose client answers
with Result-shaped objects, the way core's ``BrowserClient`` does.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from ebay_plugin import browser as browser_mod
from ebay_plugin import coreclient
from ebay_plugin import tools as tools_mod
from ebay_plugin.browser import Browser, BrowserTimeout
from ebay_plugin.history import PriceHistory
from ebay_plugin.tools import build_tools

_PAGE = {
    "found_container": True,
    "challenge": False,
    "signin_wall": False,
    "rows": [
        {
            "title": "Switch OLED",
            "url": "https://www.ebay.com/itm/1",
            "price": "$200.00",
            "shipping": "+$10.00 delivery",
        },
        {
            "title": "Switch OLED bundle",
            "url": "https://www.ebay.com/itm/2",
            "price": "$300.00",
            "shipping": "Free delivery",
        },
    ],
}


def _r(kind="ok", value=None, error="", wall=None):
    return SimpleNamespace(kind=kind, ok=kind == "ok", value=value, error=error, wall=wall)


class FakeClient:
    """Answers like core's BrowserClient. ``script`` maps a method name to a Result or a list."""

    def __init__(self, **opts):
        self.opts, self.calls = opts, []
        self.answers = {"open": _r(value={"url": "", "title": ""}), "eval_json": _r(value=_PAGE)}

    def _answer(self, name):
        a = self.answers.get(name, _r())
        return a.pop(0) if isinstance(a, list) else a

    def open(self, url, **kw):
        self.calls.append(("open", url, kw))
        return self._answer("open")

    def wait(self, **kw):
        self.calls.append(("wait", kw))
        return self._answer("wait")

    def eval_json(self, script):
        self.calls.append(("eval_json",))
        return self._answer("eval_json")

    def close_panels(self):
        self.calls.append(("close_panels",))
        return _r(value=[])

    def focus_tab(self, label=""):
        self.calls.append(("focus_tab",))
        return _r(value="switched")

    def check_wall(self, url=""):
        self.calls.append(("check_wall", url))
        return self._answer("check_wall")

    def close(self):
        return _r()


@pytest.fixture
def core(monkeypatch):
    """Install a fake ``agent_browser.client`` factory; returns the list of clients it built."""
    built = []

    def factory(**opts):
        c = FakeClient(**opts)
        built.append(c)
        return c

    monkeypatch.setattr(tools_mod, "core_factory", lambda: factory)
    return built


def _tool(name, cfg=None):
    return {t.name: t for t in build_tools(cfg or {})}[name]


# ── which path runs ──────────────────────────────────────────────────────────────────────────


def test_the_core_client_is_used_when_the_host_offers_it(core):
    out = json.loads(
        _tool("ebay_price_check", {"profile": "/p/ebay", "session": "ebay"}).invoke({"query": "switch oled"})
    )
    assert out["ok"] and out["results_found"] == 2
    (c,) = core
    # The same browser the fallback drives: the session verbatim, the same options, the same tab,
    # and nothing added — no stealth flag unless stealth is on.
    assert c.opts["session"] == "ebay" and c.opts["instance_scoped"] is False
    assert c.opts["profile"] == "/p/ebay" and c.opts["headed"] is True
    assert c.opts["browser_args"] == [] and c.opts["own_tab"] == "ebaytab"
    assert c.opts["nav_timeout_s"] == 35 and c.opts["min_interval_s"] == 1.5
    # The tools' own settle-and-classify loop reads eBay's gate chain, not core's wall probe.
    assert c.calls[0][0] == "open" and c.calls[0][2] == {"check_walls": False}
    assert ("wait", {"selector": "li.s-card, li.s-item", "timeout_s": coreclient.WAIT_TIMEOUT_S}) in c.calls


def test_stealth_sends_exactly_the_one_flag_it_always_did(core):
    _tool("ebay_price_check", {"stealth": True}).invoke({"query": "x"})
    assert core[0].opts["browser_args"] == ["--disable-blink-features=AutomationControlled"]


def test_the_fallback_runs_when_there_is_no_core_client(monkeypatch):
    monkeypatch.setattr(tools_mod, "core_factory", lambda: None)
    used = []

    class Stub:
        session, stealth = "ebay", False

        def open(self, url):
            used.append(url)

        def wait_for(self, selector, timeout_s=None):
            return True

        def eval_json(self, script):
            return _PAGE

    monkeypatch.setattr(tools_mod, "Browser", lambda **kw: Stub())
    assert json.loads(_tool("ebay_price_check").invoke({"query": "x"}))["ok"] and used


def test_a_new_factory_after_a_reload_builds_a_new_client(monkeypatch):
    factories = []

    def make_factory():
        def factory(**opts):
            return FakeClient(**opts)

        factories.append(factory)
        return factory

    current = {"f": make_factory()}
    monkeypatch.setattr(tools_mod, "core_factory", lambda: current["f"])
    check = _tool("ebay_price_check", {"cache_ttl_min": 0})
    check.invoke({"query": "x"})
    check.invoke({"query": "y"})
    current["f"] = make_factory()
    assert json.loads(check.invoke({"query": "z"}))["ok"]


def test_no_host_means_no_factory():
    assert coreclient.core_factory() is None


# ── typed failures ───────────────────────────────────────────────────────────────────────────


def test_a_timeout_is_typed_with_a_next_step(core, monkeypatch):
    def factory(**opts):
        c = FakeClient(**opts)
        c.answers["open"] = _r("timeout", error="did not load within 35s, twice")
        return c

    monkeypatch.setattr(tools_mod, "core_factory", lambda: factory)
    out = json.loads(_tool("ebay_price_check").invoke({"query": "x"}))
    assert out["ok"] is False and out["error_kind"] == "timeout"
    assert "NOT a sign-in wall" in out["next_step"] and "at most once more" in out["next_step"]


def test_a_core_wall_and_an_unavailable_cli_are_typed(monkeypatch):
    for kind, expect in (("wall", "wall"), ("unavailable", "unavailable"), ("cli_error", "browser")):

        def factory(_kind=kind, **opts):
            c = FakeClient(**opts)
            c.answers["open"] = _r(_kind, error="nope", wall=SimpleNamespace(kind="cloudflare", label="x"))
            return c

        monkeypatch.setattr(tools_mod, "core_factory", lambda f=factory: f)
        out = json.loads(_tool("ebay_price_check").invoke({"query": "x"}))
        assert out["error_kind"] == expect, (kind, out)


def _core_with(monkeypatch, **answers):
    """A fake factory whose clients start with ``answers`` (method name → Result)."""
    built = []

    def factory(**opts):
        c = FakeClient(**opts)
        c.answers.update(answers)
        built.append(c)
        return c

    monkeypatch.setattr(tools_mod, "core_factory", lambda: factory)
    return built


def test_ebays_own_walls_are_typed_as_walls(monkeypatch):
    for flag in ("signin_wall", "challenge"):
        _core_with(monkeypatch, eval_json=_r(value={**_PAGE, "rows": [], flag: True}))
        out = json.loads(_tool("ebay_price_check").invoke({"query": "x"}))
        assert out["error_kind"] == "wall" and "browser window" in out["next_step"], flag


def test_a_generic_wall_is_named_before_blaming_the_markup(monkeypatch):
    built = _core_with(
        monkeypatch,
        eval_json=_r(value={**_PAGE, "found_container": False, "rows": []}),
        check_wall=_r("wall", wall=SimpleNamespace(kind="http_403", label="an HTTP 403 Forbidden response")),
    )
    out = json.loads(_tool("ebay_price_check").invoke({"query": "x"}))
    assert out["error_kind"] == "wall" and "an HTTP 403 Forbidden response" in out["error"]
    assert any(call[0] == "check_wall" for call in built[0].calls)


def test_a_page_with_no_container_and_no_wall_is_still_the_markup_error(monkeypatch):
    _core_with(monkeypatch, eval_json=_r(value={**_PAGE, "found_container": False, "rows": []}), check_wall=_r())
    out = json.loads(_tool("ebay_price_check").invoke({"query": "x"}))
    assert out["error_kind"] == "page" and "results list" in out["error"]


def test_bad_input_is_typed(core):
    out = json.loads(_tool("ebay_price_check").invoke({"query": "   "}))
    assert out["ok"] is False and out["error_kind"] == "bad_input"


# ── the comps cache ──────────────────────────────────────────────────────────────────────────


def _opens(core):
    return sum(1 for c in core for call in c.calls if call[0] == "open")


def test_a_repeat_search_is_answered_from_the_cache(core, tmp_path):
    db = tmp_path / "h.db"
    tool = _tool("ebay_price_check", {"history_db": str(db)})
    first = json.loads(tool.invoke({"query": "Switch  OLED"}))
    second = json.loads(tool.invoke({"query": "switch oled"}))
    assert _opens(core) == 1
    assert "cached" not in first and second["cached"]["age_minutes"] == 0
    assert second["stats"] == first["stats"] and second["results_found"] == first["results_found"]
    assert any("no browser trip" in n for n in second["notes"])
    # A cache hit is not a second observation in the price history.
    assert PriceHistory(db).summary("switch oled")["observations"] == 2


def test_fresh_and_a_different_search_go_to_the_browser(core, tmp_path):
    tool = _tool("ebay_price_check", {"history_db": str(tmp_path / "h.db")})
    tool.invoke({"query": "x"})
    tool.invoke({"query": "x", "fresh": True})
    tool.invoke({"query": "x", "sold": False})
    tool.invoke({"query": "x", "condition": "used"})
    assert _opens(core) == 4


def test_the_cache_keys_on_the_filter_actually_applied_and_the_ebay_site(core, tmp_path):
    # QA (#8): condition='Used' used to search unfiltered but cache under 'used'; and a repeat on
    # another eBay domain was answered with the first site's comps.
    db = str(tmp_path / "h.db")
    tool = _tool("ebay_price_check", {"history_db": db})
    tool.invoke({"query": "x", "condition": "Used"})
    assert json.loads(tool.invoke({"query": "x", "condition": "used"}))["cached"]  # same filter
    assert "cached" not in json.loads(tool.invoke({"query": "x", "condition": "any"}))
    uk = _tool("ebay_price_check", {"history_db": db, "domain": "www.ebay.co.uk"})
    assert "cached" not in json.loads(uk.invoke({"query": "x", "condition": "used"}))
    assert _opens(core) == 3


def test_search_url_honours_condition_in_any_case():
    from ebay_plugin import extract as from_extract

    assert "LH_ItemCondition=4" in from_extract.search_url("x", condition="Used")
    assert "LH_ItemCondition" not in from_extract.search_url("x", condition="refurbished")
    assert from_extract.effective_condition(" NEW ") == "new"
    assert from_extract.effective_condition("refurbished") == "any"


def test_price_and_profit_shares_the_sold_cache(core, tmp_path):
    cfg = {"history_db": str(tmp_path / "h.db")}
    _tool("ebay_price_check", cfg).invoke({"query": "x"})
    out = json.loads(_tool("ebay_price_and_profit", cfg).invoke({"query": "x", "item_cost": 50}))
    assert out["ok"] and out["cached"] and out["at_market_prices"]
    assert _opens(core) == 1


def test_the_cache_can_be_turned_off(core, tmp_path):
    tool = _tool("ebay_price_check", {"history_db": str(tmp_path / "h.db"), "cache_ttl_min": 0})
    tool.invoke({"query": "x"})
    tool.invoke({"query": "x"})
    assert _opens(core) == 2


def test_a_failed_search_is_never_cached(core, tmp_path):
    tool = _tool("ebay_price_check", {"history_db": str(tmp_path / "h.db")})
    tool.invoke({"query": "warmup"})
    core[0].answers["open"] = [_r("timeout", error="stuck"), _r(value={})]
    assert json.loads(tool.invoke({"query": "x"}))["ok"] is False
    assert "cached" not in json.loads(tool.invoke({"query": "x"}))


def test_recent_check_respects_the_window(tmp_path):
    from datetime import UTC, datetime, timedelta

    from ebay_plugin.extract import normalize

    h = PriceHistory(tmp_path / "h.db")
    listings, _ = normalize(_PAGE["rows"], sold=True)
    old = (datetime.now(UTC) - timedelta(hours=7)).isoformat()
    h.record_check("x", "ebay", basis="sold", condition="any", listings=listings, dropped=0, meta={}, url="u", now=old)
    assert h.recent_check("x", "ebay", basis="sold", condition="any", max_age_min=360) is None
    hit = h.recent_check("x", "ebay", basis="sold", condition="any", max_age_min=480)
    assert hit and hit["age_min"] >= 419 and len(hit["listings"]) == 2
    assert h.recent_check("x", "ebay", basis="active", condition="any", max_age_min=480) is None


# ── the fallback's timeouts ──────────────────────────────────────────────────────────────────


def _fallback(monkeypatch, answer):
    monkeypatch.setattr(browser_mod.shutil, "which", lambda b: "/usr/bin/agent-browser")
    calls = []

    def fake(args, timeout, env=None):
        calls.append((args, timeout, env))
        return answer(args)

    monkeypatch.setattr(browser_mod, "_run", fake)
    return Browser(session="ebay", min_interval_s=0), calls


def test_the_fallback_wait_carries_an_explicit_timeout(monkeypatch):
    b, calls = _fallback(monkeypatch, lambda args: browser_mod.Result(True, "", ""))
    b._session_ready = True
    b.wait_for("li.s-card")
    args, timeout, env = calls[-1]
    assert args[:2] == ["agent-browser", "wait"] and env == {"AGENT_BROWSER_DEFAULT_TIMEOUT": "10000"}
    assert timeout < 30  # the subprocess never outlives the CLI's first 30 s read


def test_a_hung_fallback_navigation_recovers_once_in_a_fresh_tab(monkeypatch):
    state = {"opens": 0}

    def answer(args):
        if args[1] == "open":
            state["opens"] += 1
            if state["opens"] == 1:
                return browser_mod.Result(False, "", "timed out after 35s")
        return browser_mod.Result(True, '{"success":true,"data":{"tabs":[]}}', "")

    b, calls = _fallback(monkeypatch, answer)
    b.open("https://www.ebay.com/sch/i.html?_nkw=x")
    verbs = [a[1:3] for a, _, _ in calls]
    assert ["tab", "close"] in verbs and state["opens"] == 2
    assert all(t == 35 for a, t, _ in calls if a[1] == "open")  # nav_timeout_s, not timeout_s


def test_a_second_fallback_timeout_is_typed(monkeypatch):
    def answer(args):
        if args[1] == "open":
            return browser_mod.Result(False, "", "timed out after 35s")
        return browser_mod.Result(True, '{"success":true,"data":{"tabs":[]}}', "")

    b, _ = _fallback(monkeypatch, answer)
    with pytest.raises(BrowserTimeout) as exc:
        b.open("https://www.ebay.com/")
    assert exc.value.kind == "timeout" and "twice" in str(exc.value)

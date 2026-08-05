"""register(), the browser wrapper's failure modes, and the tool layer with a stubbed CLI.

Every test here runs with no protoAgent host and no browser.
"""

from __future__ import annotations

import json
from pathlib import Path

import ebay_plugin
import pytest
import yaml
from ebay_plugin import browser as browser_mod
from ebay_plugin.browser import Browser, BrowserError, _parse_cli_json
from ebay_plugin.tools import build_tools

ROOT = Path(__file__).resolve().parent.parent


class TestRegister:
    def test_registers_tools_and_skills(self, registry):
        ebay_plugin.register(registry)
        names = {t.name for t in registry.tools}
        assert {"ebay_price_check", "ebay_price_and_profit", "ebay_net_proceeds", "ebay_breakeven"} <= names
        assert "skills" in registry.skill_dirs

    def test_every_tool_has_a_description(self, registry):
        """An f-string 'docstring' leaves __doc__ None and the tool ships blind to the model."""
        ebay_plugin.register(registry)
        for t in registry.tools:
            assert (t.description or "").strip(), f"{t.name} has no description"

    def test_a_broken_tool_group_does_not_sink_registration(self, registry, monkeypatch):
        monkeypatch.setattr(ebay_plugin, "__name__", ebay_plugin.__name__)  # keep module identity
        import ebay_plugin.tools as tools_mod

        monkeypatch.setattr(tools_mod, "build_tools", lambda cfg: (_ for _ in ()).throw(RuntimeError("boom")))
        ebay_plugin.register(registry)  # must not raise
        assert "skills" in registry.skill_dirs  # the other contribution still landed


class TestManifest:
    def test_version_matches_pyproject(self):
        manifest = yaml.safe_load((ROOT / "protoagent.plugin.yaml").read_text())
        pyproject = (ROOT / "pyproject.toml").read_text()
        assert f'version = "{manifest["version"]}"' in pyproject

    def test_ships_disabled(self):
        """Enabling is the operator's trust decision — this drives a real browser."""
        assert yaml.safe_load((ROOT / "protoagent.plugin.yaml").read_text())["enabled"] is False

    def test_declares_the_subprocess_capability(self):
        caps = yaml.safe_load((ROOT / "protoagent.plugin.yaml").read_text())["capabilities"]
        assert caps["subprocess"] is True  # honest about shelling out to a browser

    def test_skill_is_discoverable(self):
        assert (ROOT / "skills" / "ebay-pricing" / "SKILL.md").is_file()


class TestBrowserSession:
    def test_a_hijacked_daemon_is_an_error_not_a_silent_wrong_identity(self, monkeypatch):
        """The CLI drops --profile when a daemon is already running and just warns. Accepting
        that would mean browsing as some other identity — logged out, or someone else's
        account — and reporting the results as the operator's own."""
        monkeypatch.setattr(browser_mod.shutil, "which", lambda b: "/usr/bin/agent-browser")
        monkeypatch.setattr(
            browser_mod,
            "_run",
            lambda args, timeout: browser_mod.Result(True, "⚠ --profile, --headed ignored: daemon already running", ""),
        )
        b = Browser(profile="/tmp/p")
        with pytest.raises(BrowserError, match="already running"):
            b.ensure_session()

    def test_a_missing_cli_says_how_to_install_it(self, monkeypatch):
        monkeypatch.setattr(browser_mod.shutil, "which", lambda b: None)
        with pytest.raises(BrowserError, match="npm i -g agent-browser"):
            Browser().ensure_session()

    def test_clean_launch_marks_the_session_ready_once(self, monkeypatch):
        monkeypatch.setattr(browser_mod.shutil, "which", lambda b: "/usr/bin/agent-browser")
        calls = []
        monkeypatch.setattr(
            browser_mod, "_run", lambda args, timeout: calls.append(args) or browser_mod.Result(True, "✓ Done", "")
        )
        b = Browser(profile="/tmp/p")
        b.ensure_session()
        b.ensure_session()
        assert len(calls) == 1  # not relaunched on every call


class TestCliJson:
    def test_unwraps_the_result_envelope(self):
        assert _parse_cli_json(json.dumps({"result": {"a": 1}})) == {"a": 1}

    def test_decodes_a_double_encoded_page_result(self):
        """Page scripts return JSON strings — the natural way to write a DOM extractor."""
        assert _parse_cli_json(json.dumps({"result": json.dumps({"count": 3})})) == {"count": 3}

    def test_ignores_progress_lines_before_the_payload(self):
        assert _parse_cli_json('✓ Done\n{"result": {"ok": true}}') == {"ok": True}

    def test_empty_and_garbage_raise_rather_than_returning_nothing(self):
        with pytest.raises(BrowserError):
            _parse_cli_json("")
        with pytest.raises(BrowserError):
            _parse_cli_json("not json at all")


class _StubBrowser:
    """Stands in for a real browser; returns whatever page payload a test hands it."""

    def __init__(self, payload):
        self.payload = payload
        self.opened = []

    def open(self, url):
        self.opened.append(url)

    def wait_for(self, selector, timeout_s=None):
        return True

    def eval_json(self, script):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


def _tools(monkeypatch, payload, cfg=None):
    import ebay_plugin.tools as tools_mod

    stub = _StubBrowser(payload)
    monkeypatch.setattr(tools_mod, "Browser", lambda **kw: stub)
    by_name = {t.name: t for t in build_tools(cfg or {})}
    return by_name, stub


_GOOD_PAGE = {
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


class TestToolFailureModes:
    """Each of these would otherwise surface as 'no results' — a false market claim."""

    def test_signin_wall_is_named_and_actionable(self, monkeypatch):
        tools, _ = _tools(monkeypatch, {**_GOOD_PAGE, "signin_wall": True, "rows": []})
        out = json.loads(tools["ebay_price_check"].invoke({"query": "switch"}))
        assert out["ok"] is False and "sign-in" in out["error"].lower()

    def test_challenge_page_is_named_and_handed_to_the_human(self, monkeypatch):
        """Live eBay serves /splashui/captcha ("Security Measure"), not just the
        "Pardon Our Interruption" page. Matching one and not the other reported a CAPTCHA as
        "markup changed, file a bug" — sending the operator hunting a defect instead of at
        the browser window where a human check was waiting."""
        tools, _ = _tools(monkeypatch, {**_GOOD_PAGE, "challenge": True, "rows": []})
        out = json.loads(tools["ebay_price_check"].invoke({"query": "switch"}))
        assert out["ok"] is False and "verification" in out["error"].lower()

    def test_challenge_detection_covers_every_splashui_interstitial(self):
        """Regression on the JS itself: the detector must key on the /splashui/ prefix and
        the page titles, not one hard-coded path."""
        from ebay_plugin.extract import RESULT_JS

        for marker in ("/splashui/", "pardon our interruption", "security measure", "verify yourself"):
            assert marker in RESULT_JS.lower()

    def test_broken_markup_is_a_bug_report_not_an_empty_result(self, monkeypatch):
        tools, _ = _tools(monkeypatch, {"found_container": False, "rows": []})
        out = json.loads(tools["ebay_price_check"].invoke({"query": "switch"}))
        assert out["ok"] is False and "markup" in out["error"].lower()

    def test_browser_errors_are_returned_not_raised(self, monkeypatch):
        """A raising tool shows the model a stack trace; a returned error it can act on."""
        tools, _ = _tools(monkeypatch, BrowserError("the CLI isn't on PATH"))
        out = json.loads(tools["ebay_price_check"].invoke({"query": "switch"}))
        assert out["ok"] is False and "PATH" in out["error"]


class TestPricingTools:
    def test_price_check_defaults_to_sold_and_labels_its_basis(self, monkeypatch):
        tools, stub = _tools(monkeypatch, _GOOD_PAGE)
        out = json.loads(tools["ebay_price_check"].invoke({"query": "switch oled"}))
        assert out["ok"] is True
        assert "LH_Sold=1" in stub.opened[0]
        assert "what buyers paid" in out["basis"]

    def test_active_search_says_these_are_asking_prices(self, monkeypatch):
        """The single most important label in the plugin."""
        tools, _ = _tools(monkeypatch, _GOOD_PAGE)
        out = json.loads(tools["ebay_price_check"].invoke({"query": "x", "sold": False}))
        assert "NOT sale prices" in out["basis"]

    def test_stats_include_shipping(self, monkeypatch):
        tools, _ = _tools(monkeypatch, _GOOD_PAGE)
        out = json.loads(tools["ebay_price_check"].invoke({"query": "x"}))
        assert out["stats"]["low"] == 210.0 and out["stats"]["high"] == 300.0

    def test_price_and_profit_nets_out_the_market_band(self, monkeypatch):
        tools, _ = _tools(monkeypatch, _GOOD_PAGE, cfg={"fees": {"verified_on": "2026-08-05"}})
        out = json.loads(tools["ebay_price_and_profit"].invoke({"query": "x", "item_cost": 50.0}))
        assert out["ok"] is True
        assert out["breakeven_price"] > 50.0  # fees push the floor above raw cost
        for s in out["at_market_prices"].values():
            assert s["net"] < s["sale_price"]  # fees always take something

    def test_price_and_profit_handles_no_comps_without_inventing_a_price(self, monkeypatch):
        tools, _ = _tools(monkeypatch, {**_GOOD_PAGE, "rows": []})
        out = json.loads(tools["ebay_price_and_profit"].invoke({"query": "x"}))
        assert out["ok"] is True and out["stats"] == {"count": 0} and "no sold comps" in out["note"]

    def test_net_proceeds_is_pure_arithmetic_needing_no_browser(self, monkeypatch):
        tools, stub = _tools(monkeypatch, BrowserError("browser must not be touched"))
        out = json.loads(tools["ebay_net_proceeds"].invoke({"sale_price": 100.0, "item_cost": 20.0}))
        assert out["ok"] is True and stub.opened == []


class TestSessionReuse:
    def test_all_tools_share_one_browser(self, monkeypatch):
        """A fresh Browser per call re-ran ensure_session(), so the SECOND tool call found
        the daemon this plugin had just started and tripped its own "someone else owns the
        browser" guard: the first search worked and every one after it failed. Caught live,
        not in review."""
        import ebay_plugin.tools as tools_mod

        made = []

        def _factory(**kw):
            b = _StubBrowser(_GOOD_PAGE)
            made.append(b)
            return b

        monkeypatch.setattr(tools_mod, "Browser", _factory)
        tools = {t.name: t for t in build_tools({})}
        tools["ebay_price_check"].invoke({"query": "a"})
        tools["ebay_search"].invoke({"query": "b"})
        assert len(made) == 1, "each call built its own Browser and re-launched the session"
        assert len(made[0].opened) == 2, "both searches must run through the same session"


class _FlakyBrowser(_StubBrowser):
    """First read catches the in-between page; the second sees where eBay actually landed."""

    def __init__(self, first, then):
        super().__init__(first)
        self._then = then
        self.reads = 0

    def eval_json(self, script):
        self.reads += 1
        return self.payload if self.reads == 1 else self._then


class TestRedirectRace:
    def test_a_blank_first_read_is_retried_before_blaming_the_markup(self, monkeypatch):
        """`open` returns when the requested URL loads, but eBay then bounces to sign-in. Read
        too early and you catch a page with no cards AND no signals — which reported a routine
        sign-in redirect as "eBay changed its markup, file a bug". Caught live."""
        import ebay_plugin.tools as tools_mod

        blank = {"found_container": False, "challenge": False, "signin_wall": False, "count": 0, "rows": []}
        landed = {"found_container": False, "challenge": False, "signin_wall": True, "count": 0, "rows": []}
        b = _FlakyBrowser(blank, landed)
        monkeypatch.setattr(tools_mod, "Browser", lambda **kw: b)
        monkeypatch.setattr(tools_mod, "_SETTLE_S", 0)

        tools = {t.name: t for t in build_tools({})}
        out = json.loads(tools["ebay_price_check"].invoke({"query": "x"}))
        assert b.reads == 2
        assert "sign-in" in out["error"].lower()  # correctly diagnosed, not "markup changed"

    def test_a_good_page_is_not_re_read(self, monkeypatch):
        """The retry must not tax every successful search."""
        import ebay_plugin.tools as tools_mod

        b = _FlakyBrowser({**_GOOD_PAGE, "count": 2}, {})
        monkeypatch.setattr(tools_mod, "Browser", lambda **kw: b)
        tools = {t.name: t for t in build_tools({})}
        assert json.loads(tools["ebay_price_check"].invoke({"query": "x"}))["ok"] is True
        assert b.reads == 1


class TestWaitsForResults:
    def test_navigation_waits_for_the_results_container(self, monkeypatch):
        """eBay resolves a search through a redirect chain that outlasts any sleep worth
        taking. Waiting on the container returns as soon as the page is ready and makes a
        timeout informative, instead of guessing an interval that is wrong in both
        directions."""
        import ebay_plugin.tools as tools_mod

        class _W(_StubBrowser):
            def __init__(self):
                super().__init__({**_GOOD_PAGE, "count": 2})
                self.waited = []

            def wait_for(self, selector, timeout_s=None):
                self.waited.append(selector)
                return True

        b = _W()
        monkeypatch.setattr(tools_mod, "Browser", lambda **kw: b)
        tools = {t.name: t for t in build_tools({})}
        assert json.loads(tools["ebay_price_check"].invoke({"query": "x"}))["ok"] is True
        assert b.waited and "s-card" in b.waited[0] and "s-item" in b.waited[0]

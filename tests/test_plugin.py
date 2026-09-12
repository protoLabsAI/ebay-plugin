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


_IGNORED = (
    "⚠ --profile, --headed ignored: daemon already running. "
    "Use 'agent-browser close' first to restart with new options.\n"
)
_STEALTH = "--disable-blink-features=AutomationControlled"


def _cli(monkeypatch, *, stdout="✓ Done\n", stderr="", ok=True):
    """Stub the CLI seam; returns the argv list of every call the wrapper made."""
    monkeypatch.setattr(browser_mod.shutil, "which", lambda b: "/usr/bin/agent-browser")
    calls = []

    def fake(args, timeout):
        calls.append(args)
        return browser_mod.Result(ok, stdout, stderr)

    monkeypatch.setattr(browser_mod, "_run", fake)
    return calls


def _opens(calls):
    return [c for c in calls if len(c) > 2 and c[1] == "open"]


class TestBrowserSession:
    def test_the_launch_is_an_open_with_a_url_never_a_bare_open(self, monkeypatch, tmp_path):
        """A URL-less `open` with flags makes the CLI send a SECOND, option-less launch and the
        daemon relaunches Chrome on a throwaway profile — the 0.2.0 bug that kept the profile
        from ever holding a cookie. `open about:blank` sends exactly one full-options launch."""
        calls = _cli(monkeypatch)
        Browser(profile=str(tmp_path)).ensure_session()
        (launch,) = _opens(calls)
        assert launch[2] == "about:blank"
        assert all(c[1] != "open" or c[2].startswith(("about:", "http")) for c in calls)

    def test_launch_flags_ride_on_every_navigation(self, monkeypatch, tmp_path):
        """The daemon reconciles the flags against the running browser on each open: same →
        reused, changed → relaunch with the new ones, daemon gone → respawn with them. That is
        what lets a fresh process, a subagent, or a config change converge with no bookkeeping,
        and what stops a dead daemon from coming back with NO profile."""
        calls = _cli(monkeypatch)
        b = Browser(profile=str(tmp_path), headed=True, stealth=True, min_interval_s=0)
        b.open("https://www.ebay.com/")
        b.open("https://www.ebay.com/sch/i.html?_nkw=x")
        opens = _opens(calls)
        assert [c[2] for c in opens] == [
            "about:blank",
            "https://www.ebay.com/",
            "https://www.ebay.com/sch/i.html?_nkw=x",
        ]
        for c in opens:
            assert c[c.index("--profile") + 1] == str(tmp_path)
            assert "--headed" in c
            assert c[c.index("--args") + 1] == _STEALTH
            assert c[-2:] == ["--session", "ebay"]  # scoped to our own session, never the default one

    def test_stealth_adds_the_automation_flag_only_when_asked(self, monkeypatch, tmp_path):
        calls = _cli(monkeypatch)
        Browser(profile=str(tmp_path)).ensure_session()
        assert "--args" not in _opens(calls)[0]
        calls = _cli(monkeypatch)
        Browser(profile=str(tmp_path), stealth=True).ensure_session()
        launch = _opens(calls)[0]
        assert launch[launch.index("--args") + 1] == _STEALTH

    def test_no_profile_sends_no_profile_flag(self, monkeypatch):
        calls = _cli(monkeypatch)
        Browser(profile="", headed=False).ensure_session()
        launch = _opens(calls)[0]
        assert "--profile" not in launch
        assert "--headed" not in launch

    def test_the_ignored_warning_is_noise_not_an_error(self, monkeypatch, tmp_path):
        """The CLI prints `⚠ … ignored: daemon already running` on stderr with exit 0 whenever
        a daemon exists and flags were given — client-side, before the daemon has applied
        them. 0.2.0 raised on it and told the operator to `close --all`; now it is ignored."""
        _cli(monkeypatch, stderr=_IGNORED)
        b = Browser(profile=str(tmp_path))
        b.ensure_session()
        assert b._session_ready

    def test_a_failed_launch_is_an_error_with_the_cli_message(self, monkeypatch, tmp_path):
        _cli(monkeypatch, ok=False, stderr="Failed to launch Chrome: boom")
        with pytest.raises(BrowserError, match="could not start the browser: Failed to launch Chrome: boom"):
            Browser(profile=str(tmp_path)).ensure_session()

    def test_a_missing_cli_says_how_to_install_it(self, monkeypatch):
        monkeypatch.setattr(browser_mod.shutil, "which", lambda b: None)
        with pytest.raises(BrowserError, match="npm i -g agent-browser"):
            Browser().ensure_session()

    def test_the_launch_happens_once_per_instance(self, monkeypatch, tmp_path):
        calls = _cli(monkeypatch)
        b = Browser(profile=str(tmp_path))
        b.ensure_session()
        b.ensure_session()
        assert len(_opens(calls)) == 1

    def test_close_is_scoped_to_our_session(self, monkeypatch, tmp_path):
        calls = _cli(monkeypatch)
        b = Browser(profile=str(tmp_path))
        b.ensure_session()
        b.close()
        assert calls[-1][1:] == ["close", "--session", "ebay"]
        assert not b._session_ready

    def test_nothing_is_written_into_the_profile_dir(self, monkeypatch, tmp_path):
        """The profile dir is Chrome's. 0.3.0 briefly grew a launch-marker file there; the
        daemon's own option reconciliation made it unnecessary, so it must stay gone."""
        _cli(monkeypatch)
        b = Browser(profile=str(tmp_path), min_interval_s=0)
        b.open("https://www.ebay.com/")
        b.close()
        assert list(tmp_path.iterdir()) == []


class TestCliJson:
    def test_unwraps_the_real_cli_envelope(self):
        """The CLI nests the payload under `data`, not at the top level. Unwrapping only a
        top-level "result" handed callers the whole envelope — whose .get("found_container")
        is None — so EVERY live search reported "couldn't find the results list" while the
        page had loaded fine. Looked like rate limiting for a while; it was this."""
        env = {"success": True, "data": {"origin": "https://x", "result": json.dumps({"count": 22})}, "error": None}
        assert _parse_cli_json(json.dumps(env)) == {"count": 22}

    def test_still_unwraps_a_bare_top_level_result(self):
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
        self.session = "ebay"
        self.stealth = False

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


class TestCompareAcrossMarketplaces:
    def test_a_failing_source_is_named_not_dropped(self, monkeypatch):
        """A missing source that simply vanishes turns a half-answer into a confident whole
        one — "Amazon is cheaper" when Amazon never actually answered."""
        import ebay_plugin.tools as tools_mod

        b = _StubBrowser(_GOOD_PAGE)
        monkeypatch.setattr(tools_mod, "Browser", lambda **kw: b)
        monkeypatch.setattr(
            tools_mod, "_fetch_amazon", lambda *a, **k: (_ for _ in ()).throw(tools_mod.EbayError("bot check"))
        )
        out = json.loads({t.name: t for t in build_tools({})}["compare_prices"].invoke({"query": "x"}))
        assert out["sources"]["amazon_active"]["ok"] is False
        assert "bot check" in out["sources"]["amazon_active"]["error"]
        assert out["partial"] == "only 2 of 3 sources returned data"

    def test_sold_and_asking_are_labelled_separately(self, monkeypatch):
        """The whole point of the comparison: they are different measures and must never be
        averaged into one 'market price'."""
        import ebay_plugin.tools as tools_mod

        b = _StubBrowser(_GOOD_PAGE)
        monkeypatch.setattr(tools_mod, "Browser", lambda **kw: b)
        monkeypatch.setattr(tools_mod, "_fetch_amazon", lambda *a, **k: ([], 0))
        out = json.loads({t.name: t for t in build_tools({})}["compare_prices"].invoke({"query": "x"}))
        assert "what buyers actually paid" in out["sources"]["ebay_sold"]["basis"]
        assert "asking" in out["sources"]["ebay_active"]["basis"]
        assert "asking" in out["sources"]["amazon_active"]["basis"]
        assert "not combined" in out["note"]

    def test_every_source_failing_is_an_overall_failure(self, monkeypatch):
        import ebay_plugin.tools as tools_mod

        monkeypatch.setattr(tools_mod, "Browser", lambda **kw: _StubBrowser(BrowserError("no browser")))
        monkeypatch.setattr(
            tools_mod, "_fetch_amazon", lambda *a, **k: (_ for _ in ()).throw(tools_mod.EbayError("no browser"))
        )
        out = json.loads({t.name: t for t in build_tools({})}["compare_prices"].invoke({"query": "x"}))
        assert out["ok"] is False and "every source failed" in out["error"]


class TestHistoryRecording:
    def test_the_comparison_tool_also_records(self, monkeypatch, tmp_path):
        """Only the single-source checks logged at first, so compare_prices — the tool most
        likely to be run repeatedly on a watched item — built no history at all."""
        import ebay_plugin.tools as tools_mod

        monkeypatch.setattr(tools_mod, "Browser", lambda **kw: _StubBrowser(_GOOD_PAGE))
        monkeypatch.setattr(tools_mod, "_fetch_amazon", lambda *a, **k: ([], 0))
        db = tmp_path / "h.db"
        tools = {t.name: t for t in build_tools({"history_db": str(db)})}
        tools["compare_prices"].invoke({"query": "switch oled"})

        from ebay_plugin.history import PriceHistory

        assert PriceHistory(db).summary("switch oled")["observations"] > 0

    def test_a_broken_history_store_never_fails_a_price_check(self, monkeypatch):
        """History is a nice-to-have; a search must still answer without it."""
        import ebay_plugin.tools as tools_mod

        monkeypatch.setattr(tools_mod, "Browser", lambda **kw: _StubBrowser(_GOOD_PAGE))
        tools = {t.name: t for t in build_tools({"history_db": "/nonexistent-dir/\x00/bad.db"})}
        assert json.loads(tools["ebay_price_check"].invoke({"query": "x"}))["ok"] is True


class TestLaunchConfig:
    def test_stealth_flows_from_config_to_the_browser(self, monkeypatch):
        import ebay_plugin.tools as tools_mod

        seen = {}
        monkeypatch.setattr(tools_mod, "Browser", lambda **kw: seen.update(kw) or _StubBrowser(_GOOD_PAGE))
        build_tools({"stealth": True, "profile": "/p"})
        assert seen["stealth"] is True
        assert seen["profile"] == "/p"
        build_tools({})
        assert seen["stealth"] is False  # ships off, like core's browser plugin

    def test_string_flags_from_a_settings_form_are_read_as_booleans(self, monkeypatch):
        """`bool("false")` is True. A console field or a hand-edited YAML can hand us strings."""
        import ebay_plugin.tools as tools_mod

        seen = {}
        monkeypatch.setattr(tools_mod, "Browser", lambda **kw: seen.update(kw) or _StubBrowser(_GOOD_PAGE))
        build_tools({"stealth": "false", "headed": "false"})
        assert seen["stealth"] is False
        assert seen["headed"] is False
        build_tools({"stealth": "true", "headed": "yes"})
        assert seen["stealth"] is True
        assert seen["headed"] is True
        build_tools({"stealth": "", "headed": ""})  # blank form fields are "unset", never "off"
        assert seen["stealth"] is False
        assert seen["headed"] is True  # blank headed must not mean headless — eBay refuses headless

    def test_the_manifest_ships_stealth_off(self):
        manifest = yaml.safe_load((ROOT / "protoagent.plugin.yaml").read_text())
        assert manifest["config"]["stealth"] is False

    def test_session_status_points_a_google_refusal_at_stealth(self, monkeypatch):
        by_name, _ = _tools(monkeypatch, {"signed_in": False, "greeting": ""})
        out = json.loads(by_name["ebay_session_status"].invoke({}))
        assert out["signed_in"] is False
        assert "ebay.stealth: true" in out["next_step"]
        assert "close" not in out["next_step"]  # nothing to close by hand any more

    def test_session_status_drops_the_stealth_hint_once_it_is_on(self, monkeypatch):
        by_name, stub = _tools(monkeypatch, {"signed_in": False, "greeting": ""})
        stub.stealth = True
        out = json.loads(by_name["ebay_session_status"].invoke({}))
        assert "stealth" not in out["next_step"]
        assert "Sign in to eBay" in out["next_step"]

    def test_session_status_has_no_next_step_when_signed_in(self, monkeypatch):
        by_name, _ = _tools(monkeypatch, {"signed_in": True, "greeting": "Hi Josh!"})
        out = json.loads(by_name["ebay_session_status"].invoke({}))
        assert out["signed_in"] is True
        assert out["next_step"] == ""

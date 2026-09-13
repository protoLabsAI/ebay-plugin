"""Thin wrapper around the ``agent-browser`` CLI.

Why shell out rather than depend on the ``agent_browser`` plugin: plugins must not import
each other (the event bus is the only inter-plugin channel), and this keeps the test suite
host-free — every call goes through :func:`_run`, which tests replace wholesale.

**Launch options are daemon-level, and how they are sent matters.** ``--profile``,
``--headed`` and ``--args`` describe the Chrome the daemon runs. Given on an ``open <url>``,
the CLI sends one full-options *launch* ahead of the navigation and the daemon reconciles it
against the running browser: same options → reused; different → Chrome is relaunched with the
new ones (on the same profile, so a sign-in survives); no daemon → one is started with them.
So this wrapper rides the flags on EVERY ``open <url>``. A fresh agent process, a subagent's
own tool set, a config change, a daemon that died — all converge on a browser with our
options and no bookkeeping.

What it must never do is a URL-less ``open`` with flags. That parses to an explicit launch
carrying only ``headless``, sent right after the full-options one, and the daemon dutifully
relaunches Chrome a second time — on a throwaway temp profile. That was this plugin's launch
step through 0.2.0: every "signed-in" window it ever opened was abandoned within seconds for
one on a profile nobody was signed in to, which is why the profile dir never held a cookie.
(The CLI's ``⚠ … ignored: daemon already running`` warning is printed client-side whenever a
daemon exists and flags were given; it is noise, not a signal — the daemon applied them.)
Verified live against agent-browser 0.27.1 by watching Chrome's ``--user-data-dir``.
"""

from __future__ import annotations

import contextlib
import json
import logging
import shutil
import subprocess
import time
from dataclasses import dataclass

log = logging.getLogger("protoagent.plugins.ebay")

#: Chrome's refusal when a navigation is issued to a target that is not an ordinary web page.
_BLOCKED_MARKER = "ERR_BLOCKED_BY_CLIENT"
#: Chrome 149's built-in Gemini side panel. It can open on its own in a headed window, as a
#: ``webview`` target; the pinned agent-browser makes any newly discovered target the ACTIVE
#: tab, so every later navigation goes into the panel and fails with ERR_BLOCKED_BY_CLIENT.
_GEMINI_PANEL = "gemini.google.com/glic"

#: Chrome flag that clears ``navigator.webdriver``, which Chrome sets under CDP control and
#: which Google's sign-in refuses ("this browser or app may not be secure"). The same flag
#: protoAgent's core browser plugin uses for its ``stealth`` option — and nothing more.
_STEALTH_ARGS = "--disable-blink-features=AutomationControlled"


class BrowserError(RuntimeError):
    """A browser command failed, or the CLI isn't usable. Message is operator-facing."""


@dataclass
class Result:
    ok: bool
    stdout: str
    stderr: str


def _run(args: list[str], *, timeout: float) -> Result:
    """Run the CLI once. The single seam every test stubs."""
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)  # noqa: S603
    except subprocess.TimeoutExpired:
        return Result(False, "", f"timed out after {timeout:.0f}s")
    except FileNotFoundError:
        return Result(False, "", f"{args[0]!r} not found on PATH")
    return Result(p.returncode == 0, p.stdout or "", p.stderr or "")


class Browser:
    """Drives one named ``agent-browser`` session.

    ``min_interval_s`` paces navigations. This is politeness, not evasion — it keeps a
    research loop from hammering a site faster than a person would. ``stealth`` drops the
    one automation flag that blocks Google's sign-in page; the browser still identifies
    itself as Chrome and nothing here tries to look like something it isn't.
    """

    def __init__(
        self,
        *,
        binary: str = "agent-browser",
        session: str = "ebay",
        profile: str = "",
        headed: bool = True,
        stealth: bool = False,
        timeout_s: float = 60.0,
        min_interval_s: float = 1.5,
    ):
        self.binary = binary
        self.session = session
        self.profile = profile
        self.headed = headed
        self.stealth = stealth
        self.timeout_s = timeout_s
        self.min_interval_s = min_interval_s
        self._last_nav = 0.0
        self._session_ready = False

    # ── plumbing ────────────────────────────────────────────────────────────────
    def _cmd(self, *args: str) -> list[str]:
        return [self.binary, *args, "--session", self.session]

    def available(self) -> str:
        """``""`` when the CLI is usable, else an operator-facing reason."""
        if shutil.which(self.binary) is None:
            return (
                f"the {self.binary!r} CLI isn't on PATH — install it with "
                "`npm i -g agent-browser && agent-browser install`"
            )
        return ""

    def _pace(self) -> None:
        gap = time.monotonic() - self._last_nav
        if gap < self.min_interval_s:
            time.sleep(self.min_interval_s - gap)
        self._last_nav = time.monotonic()

    def _launch_flags(self) -> list[str]:
        """The daemon-level options, sent with every ``open <url>`` (module docstring)."""
        flags: list[str] = []
        if self.profile:
            flags += ["--profile", self.profile]
        if self.headed:
            flags += ["--headed"]
        if self.stealth:
            flags += ["--args", _STEALTH_ARGS]
        return flags

    def _open_cmd(self, url: str) -> list[str]:
        return [self.binary, "open", url, *self._launch_flags(), "--session", self.session]

    # ── session ─────────────────────────────────────────────────────────────────
    def ensure_session(self) -> None:
        """Bring the browser up with our options, once per instance.

        ``open about:blank`` with the flags: the daemon starts, reuses, or relaunches as
        needed (module docstring). Never a URL-less ``open`` — that is the double launch.
        """
        if self._session_ready:
            return
        if reason := self.available():
            raise BrowserError(reason)
        res = _run(self._open_cmd("about:blank"), timeout=self.timeout_s)
        if not res.ok:
            raise BrowserError(f"could not start the browser: {(res.stderr or res.stdout).strip()}")
        self._session_ready = True

    def close(self) -> None:
        _run(self._cmd("close"), timeout=self.timeout_s)
        self._session_ready = False

    # ── tab hygiene ─────────────────────────────────────────────────────────────
    def _tabs(self) -> list[dict]:
        res = _run(self._cmd("tab", "list", "--json"), timeout=self.timeout_s)
        if not res.ok:
            return []
        try:
            payload = json.loads((res.stdout or "").strip().splitlines()[-1])
        except (ValueError, IndexError):
            return []
        tabs = (payload.get("data") or {}).get("tabs") if isinstance(payload, dict) else None
        return tabs if isinstance(tabs, list) else []

    @staticmethod
    def is_web_page(tab: dict) -> bool:
        """An ordinary tab we can navigate: a ``page`` target on http(s) or about:blank, and not
        the Gemini side panel."""
        url = str(tab.get("url") or "")
        return (
            str(tab.get("type") or "page") == "page"
            and (url.startswith(("http://", "https://")) or url == "about:blank")
            and _GEMINI_PANEL not in url
        )

    def ensure_page_tab(self) -> list[str]:
        """Make the session's active tab an ordinary web page again if something stole it.

        Closes every non-page target (the Gemini panel is the one seen in the wild), selects the
        first remaining web page, or opens a new tab when none is left. Returns the ids it
        closed — empty when the active tab was already fine, which is the common case and costs
        one ``tab list``."""
        tabs = self._tabs()
        active = next((t for t in tabs if t.get("active")), None)
        if not tabs or (active is not None and self.is_web_page(active)):
            return []
        closed = []
        for t in tabs:
            if (
                not self.is_web_page(t)
                and t.get("tabId")
                and _run(self._cmd("tab", "close", str(t["tabId"])), timeout=self.timeout_s).ok
            ):
                closed.append(str(t["tabId"]))
        pages = [t for t in tabs if self.is_web_page(t) and str(t.get("tabId")) not in closed]
        if pages:
            _run(self._cmd("tab", str(pages[0]["tabId"])), timeout=self.timeout_s)
        else:
            _run(self._cmd("tab", "new"), timeout=self.timeout_s)
        log.warning(
            "[ebay] the browser's active tab was not a web page (closed %s) — restored an ordinary tab",
            closed or "none",
        )
        return closed or ["(reselected)"]

    # ── actions ─────────────────────────────────────────────────────────────────
    def open(self, url: str) -> None:
        self.ensure_session()
        self._pace()
        self.ensure_page_tab()
        # Flags on every navigation: a daemon that died (an operator's `close`, a newer
        # CLI's idle timeout) respawns with our profile instead of a blank one.
        res = _run(self._open_cmd(url), timeout=self.timeout_s)
        if not res.ok and _BLOCKED_MARKER in (res.stderr + res.stdout) and self.ensure_page_tab():
            # Something took the tab between our check and the navigation — repaired; once more.
            res = _run(self._open_cmd(url), timeout=self.timeout_s)
        if not res.ok:
            raise BrowserError(f"could not open {url}: {(res.stderr or res.stdout).strip()}")

    def wait_for(self, selector: str, *, timeout_s: float | None = None) -> bool:
        """Block until ``selector`` appears. ``True`` if it did.

        Beats sleeping a guessed interval: eBay bounces a search through a redirect chain
        (search → verification → sign-in → back), and a fixed pause either gives up while the
        page is still resolving — reporting a working search as broken markup — or taxes every
        fast page to cover the slowest. Best-effort by design: a timeout is a legitimate
        answer (the gate pages never show results), so the caller reads the page either way
        and decides what it's looking at.
        """
        self.ensure_session()
        res = _run(self._cmd("wait", selector), timeout=timeout_s or self.timeout_s)
        return res.ok

    def eval_json(self, script: str):
        """Run ``script`` in the page and parse its JSON result.

        The script is passed on **stdin** — eBay selectors are full of quotes and brackets,
        and shell-escaping them is a bug farm. Returns whatever the script produced; a
        script that can't find what it expects returns its own ``{"error": ...}`` rather
        than a half-filled object, so a layout change reads as a failure and never as
        "no results found".
        """
        self.ensure_session()
        args = self._cmd("eval", "--stdin", "--json")
        try:
            p = subprocess.run(  # noqa: S603
                args, input=script, capture_output=True, text=True, timeout=self.timeout_s
            )
        except subprocess.TimeoutExpired:
            raise BrowserError(f"page script timed out after {self.timeout_s:.0f}s") from None
        except FileNotFoundError:
            raise BrowserError(f"{self.binary!r} not found on PATH") from None
        if p.returncode != 0:
            raise BrowserError(f"page script failed: {(p.stderr or p.stdout).strip()}")
        return _parse_cli_json(p.stdout)


def _parse_cli_json(stdout: str):
    """Pull the payload out of the CLI's ``--json`` envelope.

    The CLI wraps results as ``{"result": <value>}`` (and may print progress lines first),
    so scan for the last JSON object rather than assuming the whole of stdout is our value.
    A double-encoded string result is decoded once more — ``eval`` returns whatever the page
    returned, and returning a JSON string from page code is the natural way to write these
    extractors.
    """
    text = (stdout or "").strip()
    if not text:
        raise BrowserError("the page script returned nothing")
    payload = None
    for line in reversed(text.splitlines()):
        line = line.strip()
        if not line.startswith(("{", "[")):
            continue
        try:
            payload = json.loads(line)
            break
        except json.JSONDecodeError:
            continue
    if payload is None:
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            raise BrowserError(f"could not parse the page script's output: {text[:200]}") from None
    # The CLI's --json envelope is {"success": true, "data": {"origin": ..., "result": <value>},
    # "error": null} — the payload is nested under `data`, NOT at the top level. Unwrapping only
    # a top-level "result" handed callers the whole envelope, whose .get("found_container") is
    # None, so EVERY live search reported "couldn't find the results list" while the page had
    # loaded perfectly. It looked like rate limiting for a while; it was this.
    if isinstance(payload, dict) and isinstance(payload.get("data"), dict) and "result" in payload["data"]:
        payload = payload["data"]["result"]
    elif isinstance(payload, dict) and "result" in payload:
        payload = payload["result"]
    if isinstance(payload, str):
        # A page script that returned a plain string (not JSON) is legitimate — hand it back
        # as-is rather than treating "not JSON" as an error.
        with contextlib.suppress(json.JSONDecodeError):
            payload = json.loads(payload)
    return payload

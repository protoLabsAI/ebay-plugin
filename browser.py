"""Thin wrapper around the ``agent-browser`` CLI.

Why shell out rather than depend on the ``agent_browser`` plugin: plugins must not import
each other (the event bus is the only inter-plugin channel), and this keeps the test suite
host-free — every call goes through :func:`_run`, which tests replace wholesale.

The one non-obvious thing about the CLI: **launch options are daemon-level, not
per-command.** ``open --profile X`` on an already-running daemon prints
``⚠ --profile, --headed ignored: daemon already running`` and proceeds with whatever
profile that daemon started under. Silently accepting that would mean browsing as the wrong
identity — logged out, or worse, as some other account — so :meth:`Browser.ensure_session`
surfaces it as a real error instead. The operator either closes the daemon or points this
plugin at the profile already in use.
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

#: The CLI prints this when launch flags were dropped because a daemon was already up.
_IGNORED_MARKER = "ignored: daemon already running"


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
    research loop from hammering a site faster than a person would. Nothing here tries to
    look like something it isn't.
    """

    def __init__(
        self,
        *,
        binary: str = "agent-browser",
        session: str = "ebay",
        profile: str = "",
        headed: bool = True,
        timeout_s: float = 60.0,
        min_interval_s: float = 1.5,
    ):
        self.binary = binary
        self.session = session
        self.profile = profile
        self.headed = headed
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

    # ── session ─────────────────────────────────────────────────────────────────
    def ensure_session(self) -> None:
        """Launch the browser once, with our profile actually applied.

        Raises if the CLI dropped the launch flags because another daemon already owns the
        browser: the whole point of the profile is the logged-in eBay session, so quietly
        continuing without it would produce confidently wrong answers from a logged-out or
        unrelated identity.
        """
        if self._session_ready:
            return
        if reason := self.available():
            raise BrowserError(reason)
        args = [self.binary, "open"]
        if self.profile:
            args += ["--profile", self.profile]
        if self.headed:
            args += ["--headed"]
        args += ["--session", self.session]
        res = _run(args, timeout=self.timeout_s)
        if not res.ok:
            raise BrowserError(f"could not start the browser: {(res.stderr or res.stdout).strip()}")
        if self.profile and _IGNORED_MARKER in (res.stdout + res.stderr):
            raise BrowserError(
                "a browser daemon is already running, so this plugin's profile "
                f"({self.profile}) was ignored — it would be browsing as whatever identity that "
                "daemon started under, not your signed-in eBay session. Run `agent-browser close "
                "--all` and retry, or set ebay.profile to the profile already in use."
            )
        self._session_ready = True

    def close(self) -> None:
        _run(self._cmd("close"), timeout=self.timeout_s)
        self._session_ready = False

    # ── actions ─────────────────────────────────────────────────────────────────
    def open(self, url: str) -> None:
        self.ensure_session()
        self._pace()
        res = _run(self._cmd("open", url), timeout=self.timeout_s)
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

"""Thin wrapper around the ``agent-browser`` CLI.

Why shell out rather than depend on the ``agent_browser`` plugin: plugins must not import
each other (the event bus is the only inter-plugin channel), and this keeps the test suite
host-free — every call goes through :func:`_run`, which tests replace wholesale.

The one non-obvious thing about the CLI: **launch options are daemon-level, not
per-command.** ``open --profile X`` on an already-running daemon prints
``⚠ --profile, --headed ignored: daemon already running`` and proceeds with whatever
profile that daemon started under. Silently accepting that would mean browsing as the wrong
identity — logged out, or worse, as some other account — so :meth:`Browser.ensure_session`
surfaces it as a real error instead.

The twist: the daemon that is "already running" is usually OURS. It outlives the agent
process (a detached daemon, no idle timeout in the pinned CLI), so after an agent restart, or
from a subagent that built its own tool set, the very first eBay call used to trip this guard
on the session this plugin had launched an hour earlier — and the error told the operator to
``close --all``, killing the signed-in session it was trying to protect. So a clean launch now
leaves a small marker in the profile dir recording the options it launched with; a later
instance that finds the daemon running compares its own options to that marker, adopts the
session when they match, names the difference when they don't, and treats only a marker-less
daemon as a stranger.
"""

from __future__ import annotations

import contextlib
import json
import logging
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("protoagent.plugins.ebay")

#: The CLI prints this when launch flags were dropped because a daemon was already up.
_IGNORED_MARKER = "ignored: daemon already running"
#: Chrome flag that clears ``navigator.webdriver``, which Chrome sets under CDP control and
#: which Google's sign-in refuses ("this browser or app may not be secure"). The same flag
#: protoAgent's core browser plugin uses for its ``stealth`` option — and nothing more.
_STEALTH_ARGS = "--disable-blink-features=AutomationControlled"
#: Left in the profile dir by a clean launch; read back by a later instance that finds the
#: daemon already running, to tell "our own session" from "someone else's browser".
_MARKER_NAME = ".protoagent-ebay-session.json"


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

    # ── launch bookkeeping ──────────────────────────────────────────────────────
    def _launch_options(self) -> dict:
        """The daemon-level options this instance wants; what the marker records and compares."""
        return {
            "session": self.session,
            "profile": str(Path(self.profile).expanduser()) if self.profile else "",
            "headed": self.headed,
            "stealth": self.stealth,
        }

    def _marker_path(self) -> Path | None:
        return Path(self.profile).expanduser() / _MARKER_NAME if self.profile else None

    def _write_marker(self) -> None:
        path = self._marker_path()
        if path is None:
            return
        record = {**self._launch_options(), "launched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        try:
            path.write_text(json.dumps(record))
        except OSError as exc:  # the profile dir is Chrome's; not being able to leave a note there is not fatal
            log.warning("[ebay] could not record the browser launch in %s: %s", path, exc)

    def _read_marker(self) -> dict | None:
        path = self._marker_path()
        if path is None:
            return None
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def _clear_marker(self) -> None:
        path = self._marker_path()
        if path is not None:
            with contextlib.suppress(OSError):
                path.unlink(missing_ok=True)

    # ── session ─────────────────────────────────────────────────────────────────
    def ensure_session(self) -> None:
        """Launch the browser once, with our profile actually applied.

        Raises if the CLI dropped the launch flags because a daemon we can't account for
        already owns the session: the whole point of the profile is the logged-in eBay
        session, so quietly continuing without it would produce confidently wrong answers
        from a logged-out or unrelated identity. A daemon this plugin launched itself, with
        the same options, is adopted instead — see the module docstring.
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
        if self.stealth:
            args += ["--args", _STEALTH_ARGS]
        args += ["--session", self.session]
        res = _run(args, timeout=self.timeout_s)
        if not res.ok:
            raise BrowserError(f"could not start the browser: {(res.stderr or res.stdout).strip()}")
        if _IGNORED_MARKER in (res.stdout + res.stderr):
            if self.profile:  # no profile → no signed-in identity at stake → a shared daemon is just a browser
                self._adopt_running_session()
        else:
            self._write_marker()
        self._session_ready = True

    def _adopt_running_session(self) -> None:
        """The daemon dropped our launch flags because it was already up. Ours, or a stranger's?"""
        close_hint = f"run `agent-browser close --session {self.session}` and retry"
        marker = self._read_marker()
        if marker is None:
            raise BrowserError(
                f"a browser daemon already owns the {self.session!r} session, so this plugin's profile "
                f"({self.profile}) was ignored — it would be browsing as whatever identity that daemon "
                f"started under, not your signed-in eBay session. Either {close_hint}, or set ebay.profile "
                "to the profile already in use."
            )
        want = self._launch_options()
        diffs = [
            f"{k}: running with {marker.get(k)!r}, config wants {v!r}" for k, v in want.items() if marker.get(k) != v
        ]
        if diffs:
            raise BrowserError(
                f"the {self.session!r} browser session this plugin launched is still running with different "
                f"options ({'; '.join(diffs)}). Launch options only apply when the browser starts, so {close_hint}."
            )
        log.info(
            "[ebay] adopted the running %r browser session (launched with this profile at %s)",
            self.session,
            marker.get("launched_at", "?"),
        )

    def close(self) -> None:
        _run(self._cmd("close"), timeout=self.timeout_s)
        self._clear_marker()
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

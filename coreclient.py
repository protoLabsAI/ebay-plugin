"""Drive the browser through protoAgent core's browser client when the host offers one.

Core's ``agent_browser`` plugin offers ``agent_browser.client`` (a plugin service, ADR 0116): its
own hardened path to the same ``agent-browser`` CLI. That path has an output cap, a process-group
kill on timeout, CLI resolution (including the pinned first-use download), the argv option guard,
a lock shared with the browser tools, and typed results that tell a stuck browser from a wall.
:class:`CoreBrowser` adapts it to the small surface the tools use (``open`` / ``wait_for`` /
``eval_json`` / ``refocus``), so ``tools.py`` doesn't care which path runs.

**The fallback stays.** Fleet members on a core older than the client, or with ``agent_browser``
disabled, get ``None`` from the lookup, and the tools use :class:`browser.Browser` exactly as
before. The lookup runs on every tool call, never at ``register()``: plugins load in no fixed
order, and a reload can add or remove the provider.

Same browser either way. The session keeps its configured name verbatim
(``instance_scoped=False``), so the daemon and the profile the operator signed in to are the ones
already running. It sends the same launch options (``--profile``, ``--headed``, and the one
``stealth`` Chrome flag when that's on), so switching paths never relaunches Chrome. It uses the
same labelled tab (``ebaytab``). Stealth and anti-detection behaviour are unchanged: the client
adds no launch options of its own.
"""

from __future__ import annotations

import logging

from .browser import _STEALTH_ARGS, _TAB_LABEL, BrowserError, BrowserTimeout, BrowserUnavailable, BrowserWall

log = logging.getLogger("protoagent.plugins.ebay")

#: A selector wait. Short on purpose: a results list that hasn't appeared in 10 s usually never
#: will (a gate page), and the tools' settle loop re-reads a slow page anyway.
WAIT_TIMEOUT_S = 10.0


def core_factory():
    """The host's ``agent_browser.client`` factory, or ``None``: no host (tests, CLI), a core
    older than the client or the service seam, or ``agent_browser`` turned off."""
    try:
        from graph import sdk
    except Exception:  # noqa: BLE001 — host-free
        return None
    lookup = getattr(sdk, "service", None)
    if not callable(lookup):
        return None
    try:
        return lookup("agent_browser.client")
    except Exception:  # noqa: BLE001 — a lookup must never fail a tool call
        log.exception("[ebay] looking up the core browser client failed")
        return None


def _raise_for(result, what: str):
    """Map a failed core ``Result`` to this plugin's typed errors."""
    kind = getattr(result, "kind", "cli_error")
    error = getattr(result, "error", "") or kind
    if kind == "timeout":
        raise BrowserTimeout(f"{what}: {error}")
    if kind == "wall":
        wall = getattr(result, "wall", None)
        raise BrowserWall(f"{what}: {error}", getattr(wall, "kind", ""))
    if kind == "unavailable":
        raise BrowserUnavailable(error)
    raise BrowserError(f"{what}: {error}")


class CoreBrowser:
    """The tools' browser surface over core's client. ``factory`` is the
    ``agent_browser.client`` service; options mirror :class:`browser.Browser`'s."""

    def __init__(
        self,
        factory,
        *,
        session: str,
        profile: str,
        headed: bool,
        stealth: bool,
        timeout_s: float,
        nav_timeout_s: float,
        min_interval_s: float,
    ):
        self.stealth = stealth
        self.session = session  # tools._session_lock keys the read sequence by it
        self.client = factory(
            session=session,
            instance_scoped=False,
            profile=profile,
            headed=headed,
            browser_args=[_STEALTH_ARGS] if stealth else [],
            own_tab=_TAB_LABEL,
            timeout_s=timeout_s,
            nav_timeout_s=min(nav_timeout_s, timeout_s),
            min_interval_s=min_interval_s,
        )

    def open(self, url: str) -> None:
        # check_walls=False: the tools' own settle-and-classify loop reads eBay's gate chain
        # (search → /splashui/captcha → sign-in → back), which a probe right after the navigation
        # would catch mid-chain and wrongly call a wall. The client still recovers a hung
        # navigation once (a fresh tab) before reporting a typed timeout.
        r = self.client.open(url, check_walls=False)
        if not r.ok:
            _raise_for(r, f"could not open {url}")

    def wait_for(self, selector: str, *, timeout_s: float | None = None) -> bool:
        # Best-effort, as in Browser.wait_for: a timeout is a legitimate answer (a gate page
        # never shows results). A stuck browser shows up on the read that follows.
        return self.client.wait(selector=selector, timeout_s=timeout_s or WAIT_TIMEOUT_S).ok

    def eval_json(self, script: str):
        r = self.client.eval_json(script)
        if not r.ok:
            _raise_for(r, "page script failed")
        return r.value

    def refocus(self) -> str:
        self.client.close_panels()
        r = self.client.focus_tab()
        if not r.ok:
            _raise_for(r, "could not switch to the plugin's browser tab")
        return r.value

    def check_wall(self, url: str):
        """A generic wall (Cloudflare, a 403/429, a vendor CAPTCHA) on the current page, as
        core's wall label, or ``""``. Run only when the tools' own read found no results and no
        eBay-specific reason, to name what the page actually is."""
        r = self.client.check_wall(url)
        return r.wall.label if getattr(r, "kind", "") == "wall" and r.wall is not None else ""

    def close(self) -> None:
        self.client.close()

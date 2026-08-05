"""ebay — price research on eBay through a signed-in browser session.

``register()`` is the only place plugin code runs. Host-only imports stay lazy so the test
suite imports every module with no protoAgent host present.
"""

from __future__ import annotations

import logging

log = logging.getLogger("protoagent.plugins.ebay")


def register(registry) -> None:
    cfg = registry.config or {}

    try:
        from .tools import build_tools

        for t in build_tools(cfg):
            registry.register_tool(t)
    except Exception:  # noqa: BLE001 — one bad contribution must not sink the rest
        log.exception("[ebay] registering tools failed")

    try:
        registry.register_skill_dir("skills")
    except Exception:  # noqa: BLE001
        log.exception("[ebay] registering skills failed")

    log.info("[ebay] registered")

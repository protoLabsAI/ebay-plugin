"""Test bootstrap — import the plugin with NO protoAgent host present.

The host loads a plugin under a synthetic package; the suite does the same so the modules'
relative imports (``from .extract import ...``) resolve standalone.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PKG = "ebay_plugin"

if PKG not in sys.modules:
    _spec = importlib.util.spec_from_file_location(PKG, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    assert _spec and _spec.loader
    _mod = importlib.util.module_from_spec(_spec)
    sys.modules[PKG] = _mod
    _spec.loader.exec_module(_mod)


class FakeRegistry:
    def __init__(self, config=None):
        self.config = config or {}
        self.tools, self.skill_dirs = [], []

    def register_tool(self, t):
        self.tools.append(t)

    def register_skill_dir(self, path):
        self.skill_dirs.append(path)


@pytest.fixture
def registry():
    return FakeRegistry()

#!/usr/bin/env python3
"""Adapter package: live vendor data with automatic fixture fallback.

`with_fallback(adapter_live, live_call, fixture_call)` runs the live call when the
adapter is enabled and a key is present, and quietly falls back to the fixture value
on any absence or error, so the platform never breaks and no key is ever required.
"""

from __future__ import annotations

import sys
from typing import Any, Callable

from . import config  # noqa: F401  (re-export convenience)


def with_fallback(is_live: bool, live_call: Callable[[], Any], fixture_call: Callable[[], Any]) -> Any:
    if not is_live:
        return fixture_call()
    try:
        return live_call()
    except Exception as exc:  # noqa: BLE001 - any live failure degrades to fixtures
        sys.stderr.write(f"[cs-adapters] live call failed, using fixture: {type(exc).__name__}: {exc}\n")
        return fixture_call()

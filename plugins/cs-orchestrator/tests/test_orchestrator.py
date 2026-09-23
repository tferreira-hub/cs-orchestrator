#!/usr/bin/env python3
"""Tests for the CS Orchestrator harness, the Ways-of-Working rules, the
multi-instance suppression hook, and the grounding gate.

Run:  python3 -m pytest tests/ -v      (or)   python3 tests/test_orchestrator.py
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN))
sys.path.insert(0, str(PLUGIN / "hooks" / "scripts"))

os.environ.setdefault("CS_TODAY", "2026-09-23")

import orchestrate  # noqa: E402
from suppression import ticket_spike_on_primary, primary_instance_ids  # noqa: E402


def _result():
    return orchestrate.orchestrate()


def test_northwind_is_priority1_risk():
    """Strategic account with churn>=0.70 + Sev-1 must be Priority-1 MUST_PROTECT."""
    tasks = _result()["tasks"]
    nw = [t for t in tasks if t["account"] == "Northwind Traders" and t["priority"] == 1]
    assert nw, "Northwind should have a Priority-1 risk task"
    assert nw[0]["mandate"] == "MUST_PROTECT"
    assert "draft_message" in nw[0], "Priority-1 risk task must include a drafted action"


def test_initech_day15_payment_task():
    """High-ARR Strategic account past day 15 gets a payment task (No-Chasing rule)."""
    tasks = _result()["tasks"]
    it = [t for t in tasks if t["account"] == "Initech" and "Day-15" in t["trigger"]]
    assert it, "Initech (Strategic, $350k, 16d past due) should get a Day-15 payment task"


def test_scaled_account_no_payment_task():
    """Scaled account past day 15 must AUTO-SUSPEND, no CSM task queued."""
    tasks = _result()["tasks"]
    smallco = [t for t in tasks if t["account"] == "SmallCo"]
    assert smallco == [], "SmallCo is Scaled; day_15_plus must auto-suspend with no human task"


def test_umbrella_spike_suppressed_and_stays_expansion():
    """Umbrella's ticket spike is on a TEST instance -> suppressed; it must remain an
    expansion opportunity, NOT a false-positive risk."""
    result = _result()
    suppressed = [s for s in result["suppressed"] if s["account"] == "Umbrella Ltd"]
    assert suppressed, "Umbrella's test-instance ticket spike must be suppressed"

    umbrella_tasks = [t for t in result["tasks"] if t["account"] == "Umbrella Ltd"]
    assert any(t["mandate"] == "MUST_EXPAND" for t in umbrella_tasks), "Umbrella should be expansion"
    assert not any(t["priority"] == 1 for t in umbrella_tasks), "Umbrella must NOT be a Priority-1 risk"


def test_priority_ordering():
    """Queue must be sorted by priority ascending (P1 first)."""
    priorities = [t["priority"] for t in _result()["tasks"]]
    assert priorities == sorted(priorities)


def test_ticket_spike_requires_primary_instance():
    """A raw spike driven by a non-primary instance must not fire."""
    primary = {"p"}
    zd = {"tickets_last_7d": 18, "tickets_prev_7d": 4, "by_instance": {"p": 2, "d": 16}}
    fired, ev = ticket_spike_on_primary(zd, primary)
    assert ev["raw_spike"] is True
    assert fired is False, "spike on non-primary instance must be suppressed"


def test_grounding_gate_flags_fabricated_number():
    """The grounding gate must warn when an answer cites a churn % not in evidence."""
    gate = PLUGIN / "hooks" / "scripts" / "grounding-gate.py"
    answer = "Northwind churn is 99% and ARR is 999999."  # neither in fixtures
    proc = subprocess.run(
        [sys.executable, str(gate)], input=answer, capture_output=True, text=True,
        env={**os.environ},
    )
    assert "WARNING" in proc.stderr
    assert "99" in proc.stderr or "999999" in proc.stderr


def test_grounding_gate_passes_real_numbers():
    """Grounded figures (72 == churn 0.72, 480000 ARR) must pass clean."""
    gate = PLUGIN / "hooks" / "scripts" / "grounding-gate.py"
    answer = "Northwind churn score maps to 72% risk; ARR is 480000."
    proc = subprocess.run(
        [sys.executable, str(gate)], input=answer, capture_output=True, text=True,
        env={**os.environ},
    )
    assert "OK" in proc.stderr, proc.stderr


if __name__ == "__main__":
    # Lightweight runner if pytest isn't available.
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"FAIL {fn.__name__}: {exc}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    raise SystemExit(1 if failed else 0)

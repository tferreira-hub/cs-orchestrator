"""Tests for the sudden seat/user contraction alert (V5 UC1/UC2):
- history.usage_contraction() delta detection over a 14-day window;
- orchestrate RULE_SEAT_CONTRACTION fires (P2 MUST_PROTECT) on a >20% login/seat drop;
- the deterministic judge still PASSES (history isolated so engine + judge recompute agree);
- honest data-gap when there isn't enough history.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

PLUGIN = Path(__file__).resolve().parents[1]
PLATFORM = PLUGIN.parents[1] / "platform"
for p in (str(PLUGIN), str(PLATFORM)):
    if p not in sys.path:
        sys.path.insert(0, p)


@pytest.fixture()
def history_file(tmp_path, monkeypatch):
    """Isolate CS_HISTORY_FILE to a temp path so the engine and the judge's recompute read
    identical history, and tests never touch the real snapshot file."""
    f = tmp_path / "hist.jsonl"
    monkeypatch.setenv("CS_HISTORY_FILE", str(f))
    import importlib
    import history
    importlib.reload(history)  # pick up the patched env in module-level HISTORY_FILE
    return f, history


def _seed(history_mod, account_id, series):
    """series: list of (date, {field:value}) snapshots."""
    for day, fields in series:
        history_mod.record_snapshot(account_id, {"health": 60, **fields}, on_day=day)


def test_usage_contraction_detects_login_drop(history_file):
    f, history = history_file
    _seed(history, "au1-x", [
        ("2026-09-23", {"logins_7d": 100}),
        ("2026-10-06", {"logins_7d": 70}),   # -30% over 13 days
    ])
    c = history.usage_contraction("au1-x", window_days=14, drop_pct=20)
    assert c and c["contracted"] is True
    assert c["driver"] == "logins"
    assert c["logins_pct_change"] == -30


def test_usage_contraction_seat_drop_and_both(history_file):
    f, history = history_file
    _seed(history, "au1-s", [
        ("2026-09-25", {"logins_7d": 50, "seats": 40}),
        ("2026-10-06", {"logins_7d": 20, "seats": 25}),  # logins -60%, seats -38%
    ])
    c = history.usage_contraction("au1-s")
    assert c["contracted"] and c["driver"] == "both"


def test_usage_contraction_none_without_history(history_file):
    f, history = history_file
    _seed(history, "au1-thin", [("2026-10-06", {"logins_7d": 100})])  # single point
    assert history.usage_contraction("au1-thin") is None   # data-gap, not a false alert


def test_usage_contraction_small_drop_not_flagged(history_file):
    f, history = history_file
    _seed(history, "au1-ok", [
        ("2026-09-28", {"logins_7d": 100}),
        ("2026-10-06", {"logins_7d": 90}),   # -10%, under threshold
    ])
    c = history.usage_contraction("au1-ok")
    assert c is not None and c["contracted"] is False


def test_rule_fires_and_judge_passes(history_file, monkeypatch):
    f, history = history_file
    import orchestrate, playbook_judge, importlib
    importlib.reload(orchestrate)  # ensure fresh module state
    # Seed a clear contraction for the account.
    _seed(history, "au1-crunch", [
        ("2026-09-23", {"logins_7d": 120}),
        ("2026-10-06", {"logins_7d": 60}),   # -50%
    ])
    account = {
        "hubspot": {"name": "CrunchCo", "segment": "Strategic", "arr_usd": 50000,
                    "contacts": [], "instances": [{"instance_id": "au1-crunch", "instance_type": "primary"}]},
        "zendesk": {}, "usage": {"logins_last_7d": 60}, "churn": {}, "stripe": {},
        "onboarding": {},
    }
    monkeypatch.setenv("CS_TODAY", "2026-10-06")
    tasks, _supp = orchestrate.evaluate("au1-crunch", account)
    seat_tasks = [t for t in tasks if t["rule_id"] == orchestrate.RULE_SEAT_CONTRACTION]
    assert len(seat_tasks) == 1
    t = seat_tasks[0]
    assert t["priority"] == 2 and t["mandate"] == "MUST_PROTECT"
    assert t["evidence"]["driver"] == "logins"
    # The judge must PASS: it recomputes evaluate() against the SAME isolated history.
    verdict = playbook_judge.judge(tasks, {"au1-crunch": account})
    assert verdict["verdict"] == "PASS", verdict["violations"]


def test_rule_silent_without_history(history_file, monkeypatch):
    f, history = history_file
    import orchestrate, importlib
    importlib.reload(orchestrate)
    account = {
        "hubspot": {"name": "QuietCo", "segment": "Strategic", "arr_usd": 50000,
                    "contacts": [], "instances": [{"instance_id": "au1-quiet", "instance_type": "primary"}]},
        "zendesk": {}, "usage": {"logins_last_7d": 60}, "churn": {}, "stripe": {}, "onboarding": {},
    }
    monkeypatch.setenv("CS_TODAY", "2026-10-06")
    tasks, _ = orchestrate.evaluate("au1-quiet", account)
    assert not [t for t in tasks if t["rule_id"] == orchestrate.RULE_SEAT_CONTRACTION]

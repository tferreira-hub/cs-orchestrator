"""Tests for the ROI AI webhook telemetry feature (V5):
- HMAC signature verification (good/bad/secret-unset);
- record_roi_ai validation + idempotency on event_id;
- health_score picks up ROI AI adoption modestly and becomes computable from it alone;
- the ROI AI adoption-spike expansion rule fires on a healthy Strategic account + judge PASS.
Store isolated via CS_ROI_AI_FILE."""

from __future__ import annotations

import hashlib
import hmac
import importlib
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
def roi_env(tmp_path, monkeypatch):
    store = tmp_path / "roi.jsonl"
    monkeypatch.setenv("CS_ROI_AI_FILE", str(store))
    monkeypatch.setenv("ROI_AI_WEBHOOK_SECRET", "shh-secret")
    import engine
    importlib.reload(engine)
    return engine, store


def _sign(secret, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_signature_verify_good_bad_and_unset(roi_env, monkeypatch):
    engine, _ = roi_env
    body = b'{"hello":"world"}'
    assert engine.verify_roi_ai_signature(body, _sign("shh-secret", body)) is True
    assert engine.verify_roi_ai_signature(body, _sign("wrong", body)) is False
    assert engine.verify_roi_ai_signature(body, None) is False
    monkeypatch.delenv("ROI_AI_WEBHOOK_SECRET", raising=False)
    assert engine.roi_ai_configured() is False
    assert engine.verify_roi_ai_signature(body, _sign("shh-secret", body)) is False


def test_record_validates_and_persists(roi_env):
    engine, store = roi_env
    r = engine.record_roi_ai({"event_id": "e1", "account_ref": "AU1-1",
                              "metric_date": "2026-10-01", "adoption_score": 82, "trend": "up"})
    assert r["duplicate"] is False
    got = engine.roi_ai_for("au1-1")
    assert got["adoption_score"] == 82 and got["trend"] == "up"


def test_record_rejects_invalid(roi_env):
    engine, _ = roi_env
    for bad in (
        {"account_ref": "AU1-1", "metric_date": "2026-10-01", "adoption_score": 50},  # no event_id
        {"event_id": "e", "metric_date": "2026-10-01", "adoption_score": 50},         # no account_ref
        {"event_id": "e", "account_ref": "AU1-1", "metric_date": "nope", "adoption_score": 50},
        {"event_id": "e", "account_ref": "AU1-1", "metric_date": "2026-10-01", "adoption_score": 150},
    ):
        with pytest.raises(ValueError):
            engine.record_roi_ai(bad)


def test_record_idempotent_on_event_id(roi_env):
    engine, _ = roi_env
    base = {"event_id": "dup", "account_ref": "AU1-1", "metric_date": "2026-10-01", "adoption_score": 60}
    assert engine.record_roi_ai(base)["duplicate"] is False
    assert engine.record_roi_ai(base)["duplicate"] is True   # second time: no-op


def test_health_score_picks_up_roi_ai(roi_env):
    engine, _ = roi_env
    # An account with ONLY ROI AI telemetry is computable from it alone.
    acct = {"hubspot": {}, "zendesk": {}, "usage": {}, "churn": {}, "stripe": {},
            "jiminny": {}, "roi_ai": {"adoption_score": 90}}
    h = engine.health_score(acct)
    assert h["computable"] is True
    low = engine.health_score({**acct, "roi_ai": {"adoption_score": 5}})
    assert low["score"] < h["score"]   # very low adoption drags health down


def test_roi_ai_expansion_rule_and_judge(roi_env, monkeypatch):
    engine, _ = roi_env
    import orchestrate, playbook_judge
    importlib.reload(orchestrate)
    importlib.reload(playbook_judge)
    acct = {
        "hubspot": {"name": "GrowCo", "segment": "Strategic", "arr_usd": 200000,
                    "contacts": [], "renewal_date": None,
                    "instances": [{"instance_id": "au1-grow", "instance_type": "primary"}]},
        "zendesk": {}, "usage": {}, "churn": {}, "stripe": {}, "onboarding": {},
        "roi_ai": {"adoption_score": 88, "trend": "up", "metric_date": "2026-10-01"},
    }
    monkeypatch.setenv("CS_TODAY", "2026-10-06")
    tasks, _ = orchestrate.evaluate("au1-grow", acct)
    exp = [t for t in tasks if t["rule_id"] == orchestrate.RULE_EXPANSION_ROI_AI]
    assert len(exp) == 1 and exp[0]["priority"] == 3 and exp[0]["mandate"] == "MUST_EXPAND"
    assert playbook_judge.judge(tasks, {"au1-grow": acct})["verdict"] == "PASS"

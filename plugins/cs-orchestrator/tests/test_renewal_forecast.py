"""Tests for engine.renewal_forecast(), the deterministic renewal-outcome label
(mirrors Planhat's AI Forecast column but is rules-based and fully explainable).

Covers the three classifications and the grounding guarantees:
  - Churn Risk  (red health / ml_churn >= 0.70 / churned status)
  - Expansion   (account qualifies for a signed expansion trigger)
  - Renewal     (on track, default)
plus the no-renewal-date not-applicable case and the "no fabricated value" rule
(every rationale line traces back to a health reason / trigger).

Run:  python3 -m pytest tests/test_renewal_forecast.py -v
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1]
PLATFORM = PLUGIN.parents[1] / "platform"
sys.path.insert(0, str(PLUGIN))
sys.path.insert(0, str(PLATFORM))

os.environ.setdefault("CS_TODAY", "2026-09-23")

import engine  # noqa: E402


def _account(**hs):
    base = {"hubspot": {"renewal_date": "2026-12-01"}, "zendesk": {}, "usage": {},
            "churn": {}, "stripe": {}, "jiminny": {}}
    base["hubspot"].update(hs)
    return base


# --------------------------------------------------------------------------- #
# Churn Risk
# --------------------------------------------------------------------------- #
def test_forecast_churn_risk_from_ml_score():
    """ml_churn_score >= CHURN_RISK (0.70) forces a Churn Risk label."""
    acct = _account()
    acct["churn"] = {"ml_churn_score": 0.72}
    acct["zendesk"] = {"sev1_open": 1}
    health = engine.health_score(acct)
    fc = engine.renewal_forecast(acct, health, expansion_qualified=False)
    assert fc["applicable"] is True
    assert fc["label"] == "Churn Risk"
    assert fc["rationale"].startswith("Churn Risk —")
    # Grounded: the rationale is built from real health reasons (traceable to engine).
    assert any(r in fc["rationale"] for r in health["reasons"])


def test_forecast_churn_risk_from_red_band():
    """A red health band alone (even below the ML threshold) is a churn-risk forecast."""
    acct = _account()
    acct["churn"] = {"ml_churn_score": 0.4}
    acct["zendesk"] = {"csat_30d": 30, "sev1_open": 1}
    acct["usage"] = {"days_since_last_visit": 40, "pendo_risk_score": "high"}
    health = engine.health_score(acct)
    assert health["band"] == "red"  # precondition
    fc = engine.renewal_forecast(acct, health)
    assert fc["label"] == "Churn Risk"


def test_forecast_churn_risk_from_churned_status():
    """A churned status yields Churn Risk regardless of other signals."""
    acct = _account()
    acct["churn"] = {"churn_status": "churned"}
    health = engine.health_score(acct)
    fc = engine.renewal_forecast(acct, health)
    assert fc["label"] == "Churn Risk"
    assert "churned" in [e.lower() for e in fc["evidence"]] or \
        any("churn" in e.lower() for e in fc["evidence"])


# --------------------------------------------------------------------------- #
# Expansion
# --------------------------------------------------------------------------- #
def test_forecast_expansion_when_trigger_qualified():
    """A healthy account whose caller reports an expansion trigger fired is Expansion."""
    acct = _account()
    acct["zendesk"] = {"csat_30d": 95}
    acct["usage"] = {"days_since_last_visit": 2}
    health = engine.health_score(acct)
    assert health["band"] == "green"  # precondition: not a churn risk
    fc = engine.renewal_forecast(acct, health, expansion_qualified=True)
    assert fc["label"] == "Expansion"
    assert "expansion" in fc["rationale"].lower()


def test_forecast_churn_risk_beats_expansion():
    """Risk always wins: a red account never forecasts Expansion even if a trigger fired."""
    acct = _account()
    acct["churn"] = {"ml_churn_score": 0.9}
    health = engine.health_score(acct)
    fc = engine.renewal_forecast(acct, health, expansion_qualified=True)
    assert fc["label"] == "Churn Risk"


# --------------------------------------------------------------------------- #
# Renewal (default)
# --------------------------------------------------------------------------- #
def test_forecast_plain_renewal():
    """A healthy account with no expansion trigger is an on-track Renewal."""
    acct = _account()
    acct["zendesk"] = {"csat_30d": 90}
    acct["usage"] = {"days_since_last_visit": 3}
    health = engine.health_score(acct)
    fc = engine.renewal_forecast(acct, health, expansion_qualified=False)
    assert fc["label"] == "Renewal"
    assert fc["rationale"].startswith("Renewal —")


# --------------------------------------------------------------------------- #
# Not applicable / grounding
# --------------------------------------------------------------------------- #
def test_forecast_not_applicable_without_renewal_date():
    """No renewal date means there is nothing to forecast."""
    acct = _account()
    acct["hubspot"].pop("renewal_date")
    health = engine.health_score(acct)
    fc = engine.renewal_forecast(acct, health)
    assert fc["applicable"] is False
    assert fc["label"] is None


def test_forecast_never_fabricates_numbers():
    """Every string in the rationale must trace to an engine-produced reason or a
    deterministic trigger phrase, never an invented number."""
    acct = _account()
    acct["churn"] = {"ml_churn_score": 0.85}
    health = engine.health_score(acct)
    fc = engine.renewal_forecast(acct, health)
    # The percentage shown in evidence must equal the engine's own reason figure.
    assert "85%" in " ".join(fc["evidence"])
    assert all(isinstance(e, str) for e in fc["evidence"])

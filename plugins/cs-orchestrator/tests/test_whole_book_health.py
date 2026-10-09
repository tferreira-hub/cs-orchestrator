"""Option B: whole-book roster rows get REAL, computable health from the batched churn
signal — not just the enriched ~50 slice.

The portfolio() whole-book merge used to give every non-enriched roster row a neutral,
non-computable `_roster_band` (because those rows carried no live signals). The batched
whole-book churn query (one Redshift call, no per-account fan-out) is now merged into
each roster row and the REAL health_score runs on it, so churn-driven RAG bands populate
across all ~4,300 clients.

Guarantees pinned here:
  * a roster row WITH a batched churn signal becomes computable with a churn-driven band;
  * a churned status caps the band to red and marks churn connected;
  * a roster row WITHOUT a batched signal stays honestly non-computable (no fake green);
  * at_risk_arr rolls up the newly-computable red roster rows.

All vendor access is monkeypatched; no network. Mirrors test_whole_book_filter.py.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

PLUGIN = Path(__file__).resolve().parents[1]
PLATFORM = PLUGIN.parents[1] / "platform"
sys.path.insert(0, str(PLUGIN))
sys.path.insert(0, str(PLATFORM))


@pytest.fixture(autouse=True)
def _restore_state():
    yield
    import engine
    engine.set_principal(None)


def _enriched():
    return {
        "au1-enriched": {
            "hubspot": {"name": "Enriched Co", "segment": "Strategic", "arr_usd": 120000,
                        "csm_owner": "Owner One", "csm_owner_id": "owner-1", "contacts": [],
                        "account_id": "au1-enriched",
                        "instances": [{"instance_id": "au1-enriched", "instance_type": "primary"}]},
            "sources": {"hubspot": "live"}, "zendesk": {}, "usage": {}, "churn": {},
            "stripe": {}, "jiminny": {}, "onboarding": {},
        }
    }


def _roster():
    return [
        {"account_id": "AU1-risk", "name": "High Churn Co", "cohort": "managed",
         "segment_label": "Mid-Market", "arr_usd": 50000, "lifecycle_stage": "customer",
         "owner_id": "owner-2"},
        {"account_id": "AU1-churned", "name": "Churned Status Co", "cohort": "managed",
         "segment_label": "Corporate", "arr_usd": 30000, "lifecycle_stage": "customer",
         "owner_id": "owner-2"},
        {"account_id": "AU1-nosignal", "name": "No Signal Co", "cohort": "pooled",
         "segment_label": "Agency 1-2 Users", "arr_usd": 2000, "lifecycle_stage": "customer",
         "owner_id": None},
    ]


def _patch(monkeypatch, *, churn, metrics=None):
    import engine
    engine.orchestrate.set_account_provider(_enriched)

    class _FakeHS:
        def live(self):
            return True

        def list_all_companies(self, limit=None, cached_only=False):
            return list(_roster())

        def _owner_name(self, oid):
            return {"owner-1": "Owner One", "owner-2": "Owner Two"}.get(str(oid))

    monkeypatch.setattr(engine._src, "HUBSPOT", _FakeHS())
    # The seams portfolio() calls for the whole-book batched signals (uppercase-hyphen).
    monkeypatch.setattr(engine, "_batch_churn_for", lambda: dict(churn))
    monkeypatch.setattr(engine, "_batch_metrics_for", lambda _ids: dict(metrics or {}))
    monkeypatch.setenv("CS_REPORT_CACHE_TTL", "0")
    engine.set_principal(None)
    return engine


def test_roster_row_gets_computable_health_from_batched_ml_churn(monkeypatch):
    monkeypatch.delenv("CS_INCLUDE_CHURNED", raising=False)
    engine = _patch(monkeypatch, churn={
        "AU1-RISK": {"ml_churn_score": 0.82, "_source": "redshift-live"},
    })
    by_id = {a["account_id"]: a for a in engine.portfolio()["accounts"]}

    risk = by_id["AU1-risk"]
    # Real score ran: 100 - round(0.82*40)=100-33=67 -> amber, computable, with a reason.
    assert risk["health"]["computable"] is True
    assert risk["health"]["score"] == 67
    assert risk["health"]["band"] == "amber"
    assert any("churn risk" in r for r in risk["health"]["reasons"])
    # Churn is honestly surfaced as a connected live signal on the roster row.
    assert risk["connected"].get("churn") is True


def test_churned_status_caps_roster_row_to_red(monkeypatch):
    monkeypatch.delenv("CS_INCLUDE_CHURNED", raising=False)
    engine = _patch(monkeypatch, churn={
        "AU1-CHURNED": {"churn_status": "Churned", "_source": "redshift-live"},
    })
    by_id = {a["account_id"]: a for a in engine.portfolio()["accounts"]}
    churned = by_id["AU1-churned"]
    assert churned["health"]["computable"] is True
    assert churned["health"]["band"] == "red"
    assert churned["health"]["score"] <= 49


def test_roster_row_without_signal_stays_non_computable(monkeypatch):
    monkeypatch.delenv("CS_INCLUDE_CHURNED", raising=False)
    engine = _patch(monkeypatch, churn={
        "AU1-RISK": {"ml_churn_score": 0.82},
    })
    by_id = {a["account_id"]: a for a in engine.portfolio()["accounts"]}
    # No batched signal for this account -> honest non-computable (no fabricated green).
    nosig = by_id["AU1-nosignal"]
    assert nosig["health"]["computable"] is False
    assert nosig["connected"] == {}


def test_at_risk_arr_includes_newly_computable_red_roster_rows(monkeypatch):
    monkeypatch.delenv("CS_INCLUDE_CHURNED", raising=False)
    engine = _patch(monkeypatch, churn={
        "AU1-CHURNED": {"churn_status": "Churned"},   # red, 30k ARR
    })
    summary = engine.portfolio()["summary"]
    # The churned roster row is now computable+red, so its ARR rolls into at_risk.
    assert summary["at_risk_arr_usd"] >= 30000


def test_no_churn_source_leaves_whole_book_non_computable(monkeypatch):
    """When the churn source is not live (_batch_churn_for -> {}), every roster row stays
    non-computable — exactly the prior behaviour, no regression and no fake health."""
    monkeypatch.delenv("CS_INCLUDE_CHURNED", raising=False)
    engine = _patch(monkeypatch, churn={})
    by_id = {a["account_id"]: a for a in engine.portfolio()["accounts"]}
    for aid in ("AU1-risk", "AU1-churned", "AU1-nosignal"):
        assert by_id[aid]["health"]["computable"] is False


# --------------------------------------------------------------------------- #
# Batched warehouse metrics (NDR / seat change) make 'Not churned' computable
# --------------------------------------------------------------------------- #
def test_not_churned_gets_computable_health_from_ndr_contraction(monkeypatch):
    """A 'Not churned' account (which a bare churn status leaves non-computable) becomes
    computable from a real NDR contraction signal in the batched warehouse metrics."""
    monkeypatch.delenv("CS_INCLUDE_CHURNED", raising=False)
    engine = _patch(
        monkeypatch,
        churn={"AU1-RISK": {"churn_status": "Not churned"}},
        metrics={"AU1-RISK": {"ndr_pct": 70, "_source": "redshift-live"}},
    )
    by_id = {a["account_id"]: a for a in engine.portfolio()["accounts"]}
    risk = by_id["AU1-risk"]
    # 100 - round((100-70)*0.5)=100-15=85 -> green but COMPUTABLE from a real signal.
    assert risk["health"]["computable"] is True
    assert risk["health"]["score"] == 85
    assert any("NDR 70%" in r for r in risk["health"]["reasons"])
    assert risk["connected"].get("metrics") is True


def test_seat_contraction_penalises_and_is_computable(monkeypatch):
    monkeypatch.delenv("CS_INCLUDE_CHURNED", raising=False)
    engine = _patch(
        monkeypatch,
        churn={},
        metrics={"AU1-NOSIGNAL": {"ndr_pct": 100, "user_change": -6}},
    )
    by_id = {a["account_id"]: a for a in engine.portfolio()["accounts"]}
    row = by_id["AU1-nosignal"]
    # NDR 100 => no NDR penalty; seat change -6 => min(15, 12) = 12 penalty.
    assert row["health"]["computable"] is True
    assert row["health"]["score"] == 88
    assert any("seat change -6" in r for r in row["health"]["reasons"])


def test_ndr_expansion_is_a_modest_positive(monkeypatch):
    monkeypatch.delenv("CS_INCLUDE_CHURNED", raising=False)
    engine = _patch(
        monkeypatch,
        churn={},
        metrics={"AU1-RISK": {"ndr_pct": 120}},
    )
    by_id = {a["account_id"]: a for a in engine.portfolio()["accounts"]}
    risk = by_id["AU1-risk"]
    assert risk["health"]["computable"] is True
    assert risk["health"]["band"] == "green"
    assert any("NDR 120%" in r and "+3" in r for r in risk["health"]["reasons"])

def test_categorical_seat_contraction_from_warehouse_string(monkeypatch):
    """The warehouse user_change column is categorical ('Contracting'/'Stable'/
    'Growing'), not numeric. 'Contracting' must still register as a real seat-loss
    signal (previously silently ignored because the code only handled numbers)."""
    monkeypatch.delenv("CS_INCLUDE_CHURNED", raising=False)
    engine = _patch(
        monkeypatch,
        churn={"AU1-RISK": {"churn_status": "Not churned"}},
        metrics={"AU1-RISK": {"ndr_pct": 105, "user_change": "Contracting"}},
    )
    by_id = {a["account_id"]: a for a in engine.portfolio()["accounts"]}
    risk = by_id["AU1-risk"]
    # NDR 105 -> healthy-retention reason (no penalty); 'Contracting' -> -10.
    assert risk["health"]["computable"] is True
    assert risk["health"]["score"] == 90
    assert any("seats contracting" in r for r in risk["health"]["reasons"])
    assert any("NDR 105%" in r for r in risk["health"]["reasons"])


def test_stable_seats_only_is_computable_with_a_cited_reason(monkeypatch):
    """An account whose ONLY batched signal is user_change='Stable' (no NDR, not churned)
    must be computable AND cite 'seats stable' — never a bare, unexplained 100."""
    monkeypatch.delenv("CS_INCLUDE_CHURNED", raising=False)
    engine = _patch(
        monkeypatch,
        churn={"AU1-RISK": {"churn_status": "Not churned"}},
        metrics={"AU1-RISK": {"user_change": "Stable"}},   # ndr_pct absent
    )
    by_id = {a["account_id"]: a for a in engine.portfolio()["accounts"]}
    risk = by_id["AU1-risk"]
    assert risk["health"]["computable"] is True
    assert any("seats stable" in r for r in risk["health"]["reasons"])


def test_healthy_ndr_is_computable_with_a_cited_reason(monkeypatch):
    """NDR in the flat 100-109 band is computable AND cites a reason, so a green row is
    never a bare 100 with no evidence (grounding guarantee)."""
    monkeypatch.delenv("CS_INCLUDE_CHURNED", raising=False)
    engine = _patch(
        monkeypatch,
        churn={"AU1-RISK": {"churn_status": "Not churned"}},
        metrics={"AU1-RISK": {"ndr_pct": 103, "user_change": "Stable"}},
    )
    by_id = {a["account_id"]: a for a in engine.portfolio()["accounts"]}
    risk = by_id["AU1-risk"]
    assert risk["health"]["computable"] is True
    assert risk["health"]["reasons"]  # NOT empty — a green must cite why
    assert any("NDR 103%" in r for r in risk["health"]["reasons"])


def test_warehouse_churned_status_caps_row_red_and_suppresses_stable(monkeypatch):
    """A warehouse account_status_rms='Churned' is a hard negative: it caps health to red
    and suppresses the otherwise-positive 'seats stable' colour (an account churned in the
    warehouse is NOT healthy just because its zero seats are 'stable'). This is the bug the
    live dig exposed — 'Old Churned Revenue' accounts were scoring as green 'Stable'."""
    monkeypatch.delenv("CS_INCLUDE_CHURNED", raising=False)
    engine = _patch(
        monkeypatch,
        churn={"AU1-RISK": {"churn_status": "Not churned"}},
        metrics={"AU1-RISK": {"account_status_rms": "Churned", "user_change": "Stable"}},
    )
    by_id = {a["account_id"]: a for a in engine.portfolio()["accounts"]}
    risk = by_id["AU1-risk"]
    assert risk["health"]["computable"] is True
    assert risk["health"]["band"] == "red"
    assert risk["health"]["score"] <= 49
    assert any("warehouse status" in r.lower() for r in risk["health"]["reasons"])
    # 'seats stable' must NOT appear for a churned account.
    assert not any("seats stable" in r for r in risk["health"]["reasons"])


def test_payment_required_status_penalises(monkeypatch):
    monkeypatch.delenv("CS_INCLUDE_CHURNED", raising=False)
    engine = _patch(
        monkeypatch,
        churn={},
        metrics={"AU1-RISK": {"account_status_rms": "PaymentRequired", "user_change": "Stable"}},
    )
    by_id = {a["account_id"]: a for a in engine.portfolio()["accounts"]}
    risk = by_id["AU1-risk"]
    assert risk["health"]["computable"] is True
    assert any("PaymentRequired" in r for r in risk["health"]["reasons"])
    assert risk["health"]["score"] < 100



def test_churn_and_metrics_combine_on_same_row(monkeypatch):
    """When both batched signals exist, both contribute and both show as connected."""
    monkeypatch.delenv("CS_INCLUDE_CHURNED", raising=False)
    engine = _patch(
        monkeypatch,
        churn={"AU1-RISK": {"ml_churn_score": 0.5}},     # -20
        metrics={"AU1-RISK": {"ndr_pct": 80}},            # -10
    )
    by_id = {a["account_id"]: a for a in engine.portfolio()["accounts"]}
    risk = by_id["AU1-risk"]
    # 100 - 20 (churn) - 10 (NDR 80) = 70 -> amber, computable.
    assert risk["health"]["score"] == 70
    assert risk["health"]["computable"] is True
    assert risk["connected"].get("churn") is True
    assert risk["connected"].get("metrics") is True

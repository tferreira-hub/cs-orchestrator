"""Whole-book churn batch + honest health-computability.

The warehouse churn table (marts.int_ds_account_churn_scoring) is read once for the whole
book (Churn.batch_scores) and merged onto every roster account, so the churned cohort is
health-scorable across the whole book, not only the ~50 deeply-enriched accounts. Crucially,
a bare 'not churned' status must NOT, by itself, make an account score green: we would be
asserting health we do not actually have. Only a churned status (or a real ML score) is a
health-meaningful signal on its own.
"""
from __future__ import annotations

import sys
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1]
PLATFORM = PLUGIN.parents[1] / "platform"
for p in (str(PLUGIN), str(PLATFORM)):
    if p not in sys.path:
        sys.path.insert(0, p)


def test_churn_batch_scores_shape(monkeypatch):
    from adapters import sources
    monkeypatch.setattr(sources.Churn, "live", lambda self: True)
    monkeypatch.setattr(sources.Churn, "_client", lambda self: object())
    monkeypatch.setattr(sources.Churn, "_target_kwargs", lambda self: {})
    monkeypatch.setattr(sources.config, "env", lambda n: {
        "REDSHIFT_CHURN_TABLE": "marts.int_ds_account_churn_scoring",
        "REDSHIFT_CHURN_ID_COLUMN": "nk_ja_account",
        "REDSHIFT_CHURN_MODE": "status",
        "REDSHIFT_CHURN_STATUS_COLUMN": "calculated_churn_status",
    }.get(n))

    # Fake the Data API lifecycle: execute -> finished -> one page of 2 rows.
    class _FakeClient:
        def execute_statement(self, **k): return {"Id": "s1"}
        def describe_statement(self, Id): return {"Status": "FINISHED"}
        def get_statement_result(self, **k):
            return {"Records": [
                [{"stringValue": "AU1-1"}, {"stringValue": "Churned"}],
                [{"stringValue": "AU1-2"}, {"stringValue": "Not churned"}],
            ]}
    monkeypatch.setattr(sources.Churn, "_client", lambda self: _FakeClient())

    out = sources.CHURN.batch_scores()
    assert out["AU1-1"]["churn_status"] == "Churned"
    assert out["AU1-2"]["churn_status"] == "Not churned"
    assert out["AU1-1"]["_source"] == "redshift-live"


def test_churn_batch_empty_when_not_live(monkeypatch):
    from adapters import sources
    monkeypatch.setattr(sources.Churn, "live", lambda self: False)
    assert sources.CHURN.batch_scores() == {}


def test_churned_status_is_scorable_but_not_churned_only_is_not():
    import engine
    churned = {"hubspot": {"name": "X"}, "churn": {"churn_status": "Churned"},
               "zendesk": {}, "usage": {}, "stripe": {}, "jiminny": {}, "roi_ai": {}}
    h = engine.health_score(churned)
    assert h["computable"] is True and h["band"] == "red" and h["score"] <= 49

    # A bare 'not churned' is NOT a positive health signal: honestly not scored.
    ok = {"hubspot": {"name": "Y"}, "churn": {"churn_status": "Not churned"},
          "zendesk": {}, "usage": {}, "stripe": {}, "jiminny": {}, "roi_ai": {}}
    h2 = engine.health_score(ok)
    assert h2["computable"] is False


def test_warm_batch_churn_never_raises(monkeypatch):
    import engine
    monkeypatch.setattr(engine._src.CHURN, "batch_scores",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert engine.warm_batch_churn() == 0


def test_batch_metrics_zero_string_revenue_yields_null_ndr(monkeypatch):
    """Regression: the warehouse returns revenue as a STRING ('0.00'), which is truthy in
    Python. The old `if rev_py:` guard let 0.00/0.00 through to a ZeroDivision and silently
    nulled NDR for the whole batch; worse, churned '0.00' accounts looked like data gaps.
    NDR must be null (not a crash) when prev-year revenue is '0.00', and real when both are
    positive. account_status_rms + revenue_change must pass through."""
    from adapters import sources
    monkeypatch.setattr(sources.AccountMetrics, "live", lambda self: True)
    monkeypatch.setattr(sources.Churn, "_target_kwargs", lambda self: {})
    monkeypatch.setattr(sources.Churn, "_identifier",
                        staticmethod(lambda value, qualified=False: value))
    monkeypatch.setattr(sources.config, "env", lambda n: {
        "REDSHIFT_METRICS_TABLE": "rpt.rpt_account_ndr_monthly",
        "REDSHIFT_METRICS_ID_COLUMN": "ja_account",
    }.get(n))

    class _FakeClient:
        def execute_statement(self, **k): return {"Id": "s1"}
        def describe_statement(self, Id): return {"Status": "FINISHED"}
        def get_statement_result(self, **k):
            # columns: id, revenue, rev_py, max_daily, committed, tenure, user_change,
            #          account_status_rms, revenue_change
            return {"Records": [
                # churned, zero revenue as STRING '0.00' -> NDR null (no crash)
                [{"stringValue": "AU1-churned"}, {"stringValue": "0.00"},
                 {"stringValue": "0.00"}, {"longValue": 0}, {"stringValue": "0"},
                 {"longValue": 136}, {"stringValue": "Stable"},
                 {"stringValue": "Churned"}, {"stringValue": "Stable"}],
                # active with real revenue -> NDR computed (120/100 = 120)
                [{"stringValue": "AU1-active"}, {"stringValue": "120.00"},
                 {"stringValue": "100.00"}, {"longValue": 5}, {"stringValue": "6"},
                 {"longValue": 24}, {"stringValue": "Growing"},
                 {"stringValue": "Active"}, {"stringValue": "Growing"}],
            ]}
    monkeypatch.setattr(sources.Churn, "_client", lambda self: _FakeClient())

    out = sources.ACCOUNT_METRICS.batch_metrics([])
    assert out["AU1-CHURNED"]["ndr_pct"] is None            # '0.00'/'0.00' -> null, not a crash
    assert out["AU1-CHURNED"]["account_status_rms"] == "Churned"
    assert out["AU1-ACTIVE"]["ndr_pct"] == 120              # real NDR computed
    assert out["AU1-ACTIVE"]["account_status_rms"] == "Active"


def test_warehouse_churned_status_caps_health_red():
    """A warehouse account_status_rms='Churned' alone caps health to red and is computable
    (the live dig found 'Old Churned Revenue' accounts that must not score as healthy)."""
    import engine
    acct = {"hubspot": {"name": "Z"}, "churn": {}, "zendesk": {}, "usage": {},
            "stripe": {}, "jiminny": {}, "roi_ai": {},
            "metrics": {"account_status_rms": "Churned", "user_change": "Stable"}}
    h = engine.health_score(acct)
    assert h["computable"] is True and h["band"] == "red" and h["score"] <= 49
    assert not any("seats stable" in r for r in h["reasons"])

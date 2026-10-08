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

"""Per-account performance scorecard (CS Day-to-Day -> Accounts).

Covers the live-data-only Account Performance scorecard that replicates & improves
JobAdder's native 'Account' dashboard:

  (a) the peer benchmark rank / percentile / peer_avg math, computed in Python over a small
      SYNTHETIC cohort fed through a fake Redshift Data API client (no live network),
  (b) the honest-empty contract: a cohort of < 2 comparable peers yields {} (never a
      fabricated rank of 1-of-1),
  (c) engine.account_performance owner-scoping: a non-owner CSM gets ForbiddenError (-> 403),
  (d) the new-signal 'not_connected' passthrough: Enhanced Profile and corporate Event
      Availability have no wired upstream source yet and must surface honestly, and the
      warehouse-boolean signals (Adder Intelligence Match / AI Float) reflect the dim flags.

Style mirrors test_churn_batch.py / test_integration_contracts.py: stub the adapter's
Redshift client + target kwargs with monkeypatch and feed Data API-shaped rows; nothing
hits the network, nothing is fabricated.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

PLUGIN = Path(__file__).resolve().parents[1]
PLATFORM = PLUGIN.parents[1] / "platform"
for p in (str(PLUGIN), str(PLATFORM)):
    if p not in sys.path:
        sys.path.insert(0, p)


# --------------------------------------------------------------------------- #
# Helpers: a fake Redshift Data API client that returns a fixed column set + rows.
# The AccountPerformance adapter polls describe_statement until FINISHED then pages
# get_statement_result; we return one page shaped exactly like the real Data API.
# --------------------------------------------------------------------------- #
def _col(name):
    return {"name": name}


def _cell(v):
    if v is None:
        return {"isNull": True}
    if isinstance(v, bool):
        return {"booleanValue": v}
    if isinstance(v, int):
        return {"longValue": v}
    if isinstance(v, float):
        return {"doubleValue": v}
    return {"stringValue": str(v)}


class _FakeClient:
    def __init__(self, columns, rows):
        self._columns = columns
        self._rows = rows

    def execute_statement(self, **k):
        return {"Id": "stmt-1"}

    def describe_statement(self, Id):
        return {"Status": "FINISHED"}

    def get_statement_result(self, **k):
        return {
            "ColumnMetadata": [_col(c) for c in self._columns],
            "Records": [[_cell(v) for v in row] for row in self._rows],
        }


def _wire_acct_perf(monkeypatch, columns, rows):
    """Make AccountPerformance.benchmark() run offline against a synthetic cohort.

    Stubs live()/_client()/_target_kwargs()/_identifier() on the reused Churn connection
    helper and zeros the poll interval so the test is instant. Returns nothing; the caller
    invokes sources.ACCOUNT_PERF.benchmark(ref).
    """
    from adapters import sources
    monkeypatch.setattr(sources.AccountPerformance, "live", lambda self: True)
    monkeypatch.setattr(sources.AccountPerformance, "POLL_INTERVAL_S", 0)
    monkeypatch.setattr(sources.Churn, "_target_kwargs", lambda self: {})
    monkeypatch.setattr(sources.Churn, "_identifier",
                        staticmethod(lambda value, qualified=False: value))
    monkeypatch.setattr(sources.Churn, "_client",
                        lambda self: _FakeClient(columns, rows))


# The benchmark cohort query returns these columns (see AccountPerformance.benchmark SQL).
_BENCH_COLS = ["ja_account", "jobs_created", "ads_posted", "board_usage", "applications",
               "placements", "jobs_closed", "opportunities_created", "days_sum"]


# --------------------------------------------------------------------------- #
# (a) benchmark rank / percentile / peer_avg math on a synthetic cohort
# --------------------------------------------------------------------------- #
def test_benchmark_rank_percentile_math_on_synthetic_cohort(monkeypatch):
    from adapters import sources

    # A 4-account cohort. Target = AU1-TARGET.
    #   jobs_created: 90, 100, 50, 30  -> target(100) is the HIGHEST  => rank 1, pct 100
    #   placements:   10,  5,  5,  5   -> target(5) ties -> 3 at-or-above(>) better=1,rank 2
    #   days_sum/closed = days_to_place (LOWER is better):
    #     AU1-TARGET: 200/10 = 20.0
    #     AU1-A:      450/9  = 50.0
    #     AU1-B:      300/5  = 60.0
    #     AU1-C:      150/3  = 50.0
    #     target 20.0 is the lowest => rank 1 (lower better), pct 100.
    rows = [
        # id,          jobs, ads, boards, apps, placements, closed, opps, days_sum
        ["AU1-TARGET",  100, 200,  40,    800, 10,          10,     12,   200],
        ["AU1-A",        90, 150,  30,    600,  5,            9,      8,   450],
        ["AU1-B",        50,  80,  20,    300,  5,            5,      4,   300],
        ["AU1-C",        30,  40,  10,    200,  5,            3,      2,   150],
    ]
    _wire_acct_perf(monkeypatch, _BENCH_COLS, rows)

    bench = sources.ACCOUNT_PERF.benchmark("AU1-TARGET")
    assert bench, "expected a non-empty benchmark for a 4-account cohort"
    assert bench["_source"] == "redshift-live"
    assert bench["cohort"]["peers"] == 4

    # jobs_created: target is the single highest value of the cohort.
    jc = bench["jobs_created"]
    assert jc["rank"] == 1
    assert jc["peers"] == 4
    assert jc["percentile"] == 100           # at-or-above all 4 of 4
    assert jc["peer_avg"] == round((100 + 90 + 50 + 30) / 4, 1)  # 67.5
    assert jc["higher_is_better"] is True

    # placements: values 10,5,5,5. target=10 is strictly best -> rank 1.
    pl = bench["placements"]
    assert pl["rank"] == 1
    assert pl["percentile"] == 100
    assert pl["peer_avg"] == round((10 + 5 + 5 + 5) / 4, 1)      # 6.2

    # days_to_place (DERIVED, LOWER is better): target 20.0 is the lowest -> rank 1.
    dtp = bench["days_to_place"]
    assert dtp["rank"] == 1
    assert dtp["higher_is_better"] is False
    assert dtp["percentile"] == 100          # at-or-better than the whole cohort
    # peer_avg of days_to_place = mean(20,50,60,50) = 45.0
    assert dtp["peer_avg"] == 45.0


def test_benchmark_mid_rank_and_percentile(monkeypatch):
    """A target that is NOT the best: verify rank counts strictly-better peers + 1 and the
    percentile is the at-or-better fraction (so the math is not accidentally always 1/100)."""
    from adapters import sources
    rows = [
        # jobs_created cohort: 100, 80, 60, 40, 20  (5 accounts)
        ["AU1-TARGET",  60, 1, 1, 1, 1, 1, 1, 1],   # target jobs_created = 60 -> 3rd of 5
        ["AU1-A",      100, 1, 1, 1, 1, 1, 1, 1],
        ["AU1-B",       80, 1, 1, 1, 1, 1, 1, 1],
        ["AU1-C",       40, 1, 1, 1, 1, 1, 1, 1],
        ["AU1-D",       20, 1, 1, 1, 1, 1, 1, 1],
    ]
    _wire_acct_perf(monkeypatch, _BENCH_COLS, rows)
    bench = sources.ACCOUNT_PERF.benchmark("AU1-TARGET")
    jc = bench["jobs_created"]
    # Two peers (100, 80) strictly better -> rank 3.
    assert jc["rank"] == 3
    assert jc["peers"] == 5
    # At-or-below for higher-is-better = {60,40,20} = 3 of 5 -> 60th percentile.
    assert jc["percentile"] == 60
    assert jc["peer_avg"] == round((60 + 100 + 80 + 40 + 20) / 5, 1)  # 60.0


# --------------------------------------------------------------------------- #
# (b) honest empty when the cohort has < 2 comparable peers
# --------------------------------------------------------------------------- #
def test_benchmark_returns_empty_for_single_account_cohort(monkeypatch):
    """A lone account (no comparable peers sharing icp+account_type) must NOT fabricate a
    rank of 1-of-1 — benchmark() returns {} so the UI shows an honest 'no peer cohort'."""
    from adapters import sources
    rows = [
        ["AU1-TARGET", 100, 200, 40, 800, 10, 10, 12, 200],
    ]
    _wire_acct_perf(monkeypatch, _BENCH_COLS, rows)
    assert sources.ACCOUNT_PERF.benchmark("AU1-TARGET") == {}


def test_benchmark_empty_when_not_live(monkeypatch):
    from adapters import sources
    monkeypatch.setattr(sources.AccountPerformance, "live", lambda self: False)
    assert sources.ACCOUNT_PERF.benchmark("AU1-TARGET") == {}


def test_benchmark_per_metric_none_when_fewer_than_two_values_present(monkeypatch):
    """A metric present on < 2 cohort members is honestly None (cannot rank a single value)
    even when the cohort itself is >= 2 and other metrics rank fine."""
    from adapters import sources
    # Two accounts; only the target has a non-null board_usage, so board_usage -> None,
    # while jobs_created (present on both) ranks.
    rows = [
        ["AU1-TARGET", 100, 200, 40,   800, 10, 10, 12, 200],
        ["AU1-A",       50, 150, None, 600,  5,  9,  8, 450],
    ]
    _wire_acct_perf(monkeypatch, _BENCH_COLS, rows)
    bench = sources.ACCOUNT_PERF.benchmark("AU1-TARGET")
    assert bench["cohort"]["peers"] == 2
    assert bench["board_usage"] is None            # only one value present -> honest None
    assert isinstance(bench["jobs_created"], dict)  # two values present -> ranked
    assert bench["jobs_created"]["rank"] == 1


# --------------------------------------------------------------------------- #
# (c) engine.account_performance owner-scoping: non-owner CSM -> ForbiddenError
# --------------------------------------------------------------------------- #
def _fake_accounts():
    def mk(aid, owner_id, name):
        return {aid: {
            "hubspot": {"name": name, "arr_usd": 100000, "csm_owner_id": owner_id,
                        "contacts": [], "instances": [{"instance_id": aid}]},
            "sources": {"hubspot": "live"}, "zendesk": {}, "usage": {}, "churn": {},
            "stripe": {}, "jiminny": {},
        }}
    data = {}
    data.update(mk("au1-1", "owner-A", "Alpha"))
    data.update(mk("au1-3", "owner-B", "Bravo"))
    return data


def _offline_all_sources(monkeypatch):
    """Force every connected system OFFLINE by patching the adapter CLASSES (never the
    shared singleton instances — an instance attr would leak into later tests and shadow
    their class-level live() patch). With nothing live, account_performance assembles an
    honest all-'not_connected' scorecard."""
    from adapters import sources
    monkeypatch.setattr(sources.AccountPerformance, "live", lambda self: False)
    monkeypatch.setattr(sources.HubSpot, "live", lambda self: False)
    monkeypatch.setattr(sources.Zendesk, "live", lambda self: False)


def test_account_performance_forbidden_for_non_owner_csm(monkeypatch):
    import engine, dataaccess
    monkeypatch.setattr(dataaccess, "all_accounts", _fake_accounts)
    # Keep the live warehouse out of this unit test — scoping must reject BEFORE any read.
    _offline_all_sources(monkeypatch)

    # CSM owner-A tries to open owner-B's account -> hard 403.
    engine.set_principal({"email": "a@x.com", "name": "A", "role": "csm", "owner_id": "owner-A"})
    with pytest.raises(engine.ForbiddenError):
        engine.account_performance("au1-3")

    # Owner-A opening their OWN account does not raise ForbiddenError (returns a scorecard;
    # with no live source every signal is honestly not_connected/None).
    card = engine.account_performance("au1-1")
    assert card["account_id"] == "au1-1"
    engine.set_principal(None)


def test_account_performance_admin_can_view_any(monkeypatch):
    import engine, dataaccess
    monkeypatch.setattr(dataaccess, "all_accounts", _fake_accounts)
    _offline_all_sources(monkeypatch)
    engine.set_principal({"email": "boss@x.com", "name": "Boss", "role": "admin", "owner_id": None})
    card = engine.account_performance("au1-3")   # not owned, but admin
    assert card["account_id"] == "au1-3"
    engine.set_principal(None)


# --------------------------------------------------------------------------- #
# (d) new-signal 'not_connected' passthrough
# --------------------------------------------------------------------------- #
def test_new_signals_not_connected_passthrough(monkeypatch):
    """Enhanced Profile and corporate Event Availability have no wired upstream source yet:
    they must come back as an honest 'not_connected' (never fabricated as on/off). The
    warehouse-boolean signals reflect the dim flags when the dimension IS connected."""
    import engine, dataaccess
    from adapters import sources
    monkeypatch.setattr(dataaccess, "all_accounts", _fake_accounts)
    monkeypatch.setattr(engine, "can_view_account", lambda account_id: True)

    # Warehouse dim IS connected and carries the two AI booleans; performance/benchmark/
    # feature_usage/HubSpot/Zendesk are all offline -> honest gaps. Patch the CLASSES so no
    # instance attr leaks onto the shared singletons (which would break later tests).
    monkeypatch.setattr(sources.AccountPerformance, "live", lambda self: True)
    monkeypatch.setattr(sources.AccountPerformance, "dimension", lambda self, ref: {
        "account_name": "Acme Corp", "account_status": "Active", "account_type": "Agency",
        "account_kind": "Corporate", "tier_name": "Pro", "country": "AU",
        "is_ai_matching_enabled": True, "is_floats_enabled": False,
        "stripe_customer_id": None, "global_customer_id": None, "_source": "redshift-live",
    })
    monkeypatch.setattr(sources.AccountPerformance, "performance", lambda self, ref: {})
    monkeypatch.setattr(sources.AccountPerformance, "benchmark", lambda self, ref: {})
    monkeypatch.setattr(sources.AccountPerformance, "feature_usage", lambda self, ref: {})
    monkeypatch.setattr(sources.HubSpot, "live", lambda self: False)
    monkeypatch.setattr(sources.Zendesk, "live", lambda self: False)

    engine.set_principal(None)
    card = engine.account_performance("au1-1")
    sig = card["signals"]

    # Unwired signals: honest not_connected, never fabricated on/off.
    assert sig["enhanced_profile"]["status"] == "not_connected"
    assert "enabled" not in sig["enhanced_profile"]
    assert sig["event_availability"]["status"] == "not_connected"
    assert "enabled" not in sig["event_availability"]
    assert sig["enhanced_profile"].get("note")   # carries the honest explanatory note

    # Warehouse-boolean signals reflect the connected dim flags.
    assert sig["adder_intelligence_match"]["status"] == "enabled"
    assert sig["adder_intelligence_match"]["enabled"] is True
    assert sig["ai_float"]["status"] == "disabled"
    assert sig["ai_float"]["enabled"] is False

    # The scorecard honestly reports which sources were connected.
    conn = card["connected"]
    assert conn["warehouse_dimension"] is True
    assert conn["warehouse_performance"] is False
    assert conn["benchmark"] is False
    assert conn["zendesk"] is False
    assert card["tickets"]["status"] == "not_connected"


def test_new_signals_not_connected_when_dim_absent(monkeypatch):
    """When the warehouse dim is NOT connected, even the AI-boolean signals are honestly
    'not_connected' (not assumed off), and the whole scorecard still assembles."""
    import engine, dataaccess
    monkeypatch.setattr(dataaccess, "all_accounts", _fake_accounts)
    monkeypatch.setattr(engine, "can_view_account", lambda account_id: True)
    # Nothing live at all: no 404 (we cannot assert 'unknown' with no source), honest gaps.
    _offline_all_sources(monkeypatch)

    engine.set_principal(None)
    card = engine.account_performance("au1-1")
    sig = card["signals"]
    assert sig["adder_intelligence_match"]["status"] == "not_connected"
    assert sig["ai_float"]["status"] == "not_connected"
    assert sig["enhanced_profile"]["status"] == "not_connected"
    assert sig["event_availability"]["status"] == "not_connected"
    assert card["connected"]["warehouse_dimension"] is False


# --------------------------------------------------------------------------- #
# (e) Users identity field (native-dashboard 'Users: 168 (-3)' parity)
# --------------------------------------------------------------------------- #
def test_users_block_populated_from_metrics(monkeypatch):
    """The identity.users block mirrors JobAdder's native 'Users: 168 (-3)' header,
    sourced from the warehouse metrics adapter (active_users + user_change). Honest None
    when the metrics warehouse is not connected."""
    import engine, dataaccess
    from adapters import sources
    monkeypatch.setattr(dataaccess, "all_accounts", _fake_accounts)
    monkeypatch.setattr(engine, "can_view_account", lambda account_id: True)
    # Metrics warehouse connected and returning native user counts + delta.
    monkeypatch.setattr(sources.AccountMetrics, "live", lambda self: True)
    monkeypatch.setattr(sources.AccountMetrics, "metrics", lambda self, ref: {
        "active_users": 168, "committed_users": 200, "user_change": -3,
        "user_utilization_pct": 84, "_source": "redshift-live",
    })
    # Everything else offline -> honest gaps elsewhere; users must still populate.
    monkeypatch.setattr(sources.AccountPerformance, "live", lambda self: False)
    monkeypatch.setattr(sources.HubSpot, "live", lambda self: False)
    monkeypatch.setattr(sources.Zendesk, "live", lambda self: False)

    engine.set_principal(None)
    card = engine.account_performance("au1-1")
    users = card["identity"]["users"]
    assert users is not None
    assert users["active"] == 168
    assert users["committed"] == 200
    assert users["change"] == -3
    assert users["utilization_pct"] == 84


def test_users_block_none_when_metrics_offline(monkeypatch):
    """No metrics warehouse -> identity.users is honestly None (never fabricated)."""
    import engine, dataaccess
    from adapters import sources
    monkeypatch.setattr(dataaccess, "all_accounts", _fake_accounts)
    monkeypatch.setattr(engine, "can_view_account", lambda account_id: True)
    _offline_all_sources(monkeypatch)
    monkeypatch.setattr(sources.AccountMetrics, "live", lambda self: False)

    engine.set_principal(None)
    card = engine.account_performance("au1-1")
    assert card["identity"]["users"] is None


# --------------------------------------------------------------------------- #
# (f) roster fallback: a selectable account absent from the warehouse must not 404
# --------------------------------------------------------------------------- #
def test_roster_only_account_renders_identity_not_404(monkeypatch):
    """An account the user can pick (in the whole-book roster) but with NO warehouse dim
    and NO per-account HubSpot row must render an identity-only scorecard with an honest
    data_note — never a bare 404/KeyError."""
    import engine, dataaccess
    from adapters import sources
    monkeypatch.setattr(dataaccess, "all_accounts", _fake_accounts)
    monkeypatch.setattr(engine, "can_view_account", lambda account_id: True)
    # Warehouse 'live' but returns nothing for this id; per-account HubSpot also empty.
    monkeypatch.setattr(sources.AccountPerformance, "live", lambda self: True)
    monkeypatch.setattr(sources.AccountPerformance, "dimension", lambda self, ref: {})
    monkeypatch.setattr(sources.AccountPerformance, "performance", lambda self, ref: {})
    monkeypatch.setattr(sources.AccountPerformance, "benchmark", lambda self, ref: {})
    monkeypatch.setattr(sources.AccountPerformance, "feature_usage", lambda self, ref: {})
    monkeypatch.setattr(sources.HubSpot, "live", lambda self: False)
    monkeypatch.setattr(sources.Zendesk, "live", lambda self: False)
    monkeypatch.setattr(sources.AccountMetrics, "live", lambda self: False)
    # The whole-book roster DOES contain the account (what the picker lists).
    monkeypatch.setattr(engine, "full_roster", lambda: {"companies": [
        {"account_id": "au9-1", "name": "1300 Hired", "arr_usd": 42000, "country": "AU"},
    ]})

    engine.set_principal(None)
    card = engine.account_performance("au9-1")   # must NOT raise KeyError
    assert card["account_id"] == "au9-1"
    assert card["identity"]["name"] == "1300 Hired"
    assert card["identity"]["arr_usd"] == 42000
    assert card["roster_only"] is True
    assert card["data_note"]
    # Honest gaps for the missing warehouse data.
    assert card["connected"]["warehouse_performance"] is False


def test_perf_present_dim_missing_shows_profile_note(monkeypatch):
    """An account with PERFORMANCE rows but no DIMENSION row (e.g. '1300 Hired') must show
    its live metrics, NOT claim 'no warehouse performance data', and surface an accurate
    'profile/benchmark unavailable' note. roster_only must be False (perf is present)."""
    import engine, dataaccess
    from adapters import sources
    monkeypatch.setattr(dataaccess, "all_accounts", _fake_accounts)
    monkeypatch.setattr(engine, "can_view_account", lambda account_id: True)
    monkeypatch.setattr(sources.AccountPerformance, "live", lambda self: True)
    monkeypatch.setattr(sources.AccountPerformance, "dimension", lambda self, ref: {})   # no dim
    monkeypatch.setattr(sources.AccountPerformance, "performance", lambda self, ref: {
        "metrics": {"jobs_created": 23, "ads_posted": 52}, "previous": {"jobs_created": 16},
        "window": {"months": 12, "end": "2026-10"}, "_source": "redshift-live",
    })
    monkeypatch.setattr(sources.AccountPerformance, "benchmark", lambda self, ref: {})
    monkeypatch.setattr(sources.AccountPerformance, "feature_usage", lambda self, ref: {})
    monkeypatch.setattr(sources.HubSpot, "live", lambda self: False)
    monkeypatch.setattr(sources.Zendesk, "live", lambda self: False)
    monkeypatch.setattr(sources.AccountMetrics, "live", lambda self: False)
    monkeypatch.setattr(engine, "full_roster", lambda: {"companies": [
        {"account_id": "au9-2", "name": "1300 Hired", "arr_usd": 3396, "country": "Australia"},
    ]})
    engine.set_principal(None)
    card = engine.account_performance("au9-2")
    assert card["identity"]["name"] == "1300 Hired"
    assert card["performance"]["metrics"]["jobs_created"] == 23   # live metrics shown
    assert card["has_warehouse_performance"] is True
    assert card["has_warehouse_profile"] is False
    assert card["roster_only"] is False                          # NOT a bare roster-only
    assert card["data_note"] and "profile" in card["data_note"].lower()


def test_unknown_account_still_404s_when_not_in_roster(monkeypatch):
    """An id in NO source and NOT in the roster is genuinely unknown -> KeyError (404)."""
    import engine, dataaccess
    from adapters import sources
    monkeypatch.setattr(dataaccess, "all_accounts", _fake_accounts)
    monkeypatch.setattr(engine, "can_view_account", lambda account_id: True)
    monkeypatch.setattr(sources.AccountPerformance, "live", lambda self: True)
    monkeypatch.setattr(sources.AccountPerformance, "dimension", lambda self, ref: {})
    monkeypatch.setattr(sources.HubSpot, "live", lambda self: False)
    monkeypatch.setattr(engine, "full_roster", lambda: {"companies": []})
    engine.set_principal(None)
    with pytest.raises(KeyError):
        engine.account_performance("au9-nope")

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

    # Unwired signals: honest, never fabricated on/off.
    # Enhanced Profile has no configured warehouse column here -> not_connected.
    assert sig["enhanced_profile"]["status"] == "not_connected"
    assert "enabled" not in sig["enhanced_profile"]
    assert sig["enhanced_profile"].get("note")   # carries the honest explanatory note
    # Event Availability is a Corporate-only feature; this account_type is 'Agency', so it is
    # honestly reported as not_applicable (never a fabricated on/off). enabled must be None.
    assert sig["event_availability"]["status"] == "not_applicable"
    assert sig["event_availability"].get("enabled") is None
    assert sig["event_availability"].get("note")

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


def test_event_availability_live_for_corporate_when_column_configured(monkeypatch):
    """When CS_EVENT_AVAIL_COL / CS_ENHANCED_PROFILE_COL are configured and the dim carries
    those flags for a Corporate account, the signals go LIVE (enabled/disabled) instead of
    not_connected — proving the opt-in warehouse-column wiring works end to end."""
    import engine, dataaccess
    from adapters import sources
    monkeypatch.setattr(dataaccess, "all_accounts", _fake_accounts)
    monkeypatch.setattr(engine, "can_view_account", lambda account_id: True)
    monkeypatch.setenv("CS_ENHANCED_PROFILE_COL", "is_enhanced_profile")
    monkeypatch.setenv("CS_EVENT_AVAIL_COL", "is_event_available")
    monkeypatch.setattr(sources.AccountPerformance, "live", lambda self: True)
    # Dim as the engine would receive it after the adapter surfaced the optional columns.
    monkeypatch.setattr(sources.AccountPerformance, "dimension", lambda self, ref: {
        "account_name": "BigCorp", "account_status": "Active", "account_type": "Corporate",
        "account_kind": "Corporate", "tier_name": "Pro", "country": "AU",
        "is_ai_matching_enabled": True, "is_floats_enabled": True,
        "enhanced_profile_enabled": True, "event_availability_enabled": False,
        "stripe_customer_id": None, "global_customer_id": None, "_source": "redshift-live",
    })
    monkeypatch.setattr(sources.AccountPerformance, "performance", lambda self, ref: {})
    monkeypatch.setattr(sources.AccountPerformance, "benchmark", lambda self, ref, **kw: {})
    monkeypatch.setattr(sources.AccountPerformance, "feature_usage", lambda self, ref: {})
    monkeypatch.setattr(sources.HubSpot, "live", lambda self: False)
    monkeypatch.setattr(sources.Zendesk, "live", lambda self: False)
    engine.set_principal(None)
    sig = engine.account_performance("au1-1")["signals"]
    assert sig["enhanced_profile"]["status"] == "enabled"
    assert sig["enhanced_profile"]["enabled"] is True
    # Corporate account => event availability applies and reflects the (false) flag.
    assert sig["event_availability"]["status"] == "disabled"
    assert sig["event_availability"]["enabled"] is False


def test_save_and_list_report_schedule(tmp_path, monkeypatch):
    """A quarterly report schedule persists, is listed back, and is honest about whether an
    email provider is connected (never claims a send). Owner-scoping is enforced."""
    import engine, dataaccess
    monkeypatch.setattr(dataaccess, "all_accounts", _fake_accounts)
    monkeypatch.setattr(engine, "can_view_account", lambda account_id: True)
    monkeypatch.setenv("CS_REPORT_SCHEDULE_FILE", str(tmp_path / "sched.jsonl"))
    monkeypatch.delenv("CS_EMAIL_PROVIDER", raising=False)
    engine.set_principal(None)

    rec = engine.save_report_schedule("au1-1", "boss@company.com", cadence="quarterly")
    assert rec["account_id"] == "au1-1"
    assert rec["cadence"] == "quarterly"
    assert rec["email_provider_connected"] is False
    assert "nothing is sent" in rec["delivery"].lower()

    listed = engine.list_report_schedules()
    assert listed["count"] == 1
    assert listed["schedules"][0]["email"] == "boss@company.com"

    # Invalid email rejected; bad cadence rejected.
    import pytest as _pt
    with _pt.raises(ValueError):
        engine.save_report_schedule("au1-1", "not-an-email", cadence="quarterly")
    with _pt.raises(ValueError):
        engine.save_report_schedule("au1-1", "boss@company.com", cadence="weekly")


def test_save_report_schedule_owner_scoped(tmp_path, monkeypatch):
    """A CSM cannot schedule a report for an account they don't own (ForbiddenError)."""
    import engine, dataaccess
    import pytest as _pt
    monkeypatch.setattr(dataaccess, "all_accounts", _fake_accounts)
    monkeypatch.setenv("CS_REPORT_SCHEDULE_FILE", str(tmp_path / "sched.jsonl"))
    engine.set_principal({"email": "a@x.com", "name": "A", "role": "csm", "owner_id": "owner-A"})
    with _pt.raises(engine.ForbiddenError):
        engine.save_report_schedule("au1-3", "a@x.com", cadence="quarterly")  # not owned
    engine.set_principal(None)
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


def test_perf_present_dim_missing_but_benchmark_ranked_does_not_claim_hidden(monkeypatch):
    """Regression for the data-provenance contradiction: an account with performance rows
    and NO warehouse dimension can STILL be ranked (the cohort query falls back to matching
    on icp when account_type is NULL). When rankings ARE produced, the banner must NOT say
    'rankings are hidden' — the old code computed the note before the benchmark was built
    and always claimed hidden, contradicting the rankings shown on screen."""
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
    # Benchmark returns a real ranked cohort despite the missing dim row.
    monkeypatch.setattr(sources.AccountPerformance, "benchmark",
                        lambda self, ref, **kw: {
                            "cohort": {"peers": 9498},
                            "jobs_created": {"rank": 1767, "peers": 9498, "percentile": 81,
                                             "peer_avg": 24.1},
                            "applied_filters": {"peer_group": "icp+account_type"},
                        })
    monkeypatch.setattr(sources.AccountPerformance, "feature_usage", lambda self, ref: {})
    monkeypatch.setattr(sources.HubSpot, "live", lambda self: False)
    monkeypatch.setattr(sources.Zendesk, "live", lambda self: False)
    monkeypatch.setattr(sources.AccountMetrics, "live", lambda self: False)
    monkeypatch.setattr(engine, "full_roster", lambda: {"companies": [
        {"account_id": "au9-2", "name": "1300 Hired", "arr_usd": 3396, "country": "Australia"},
    ]})
    engine.set_principal(None)
    card = engine.account_performance("au9-2")
    assert card["has_warehouse_performance"] is True
    assert card["has_warehouse_profile"] is False
    assert card["has_benchmark"] is True                      # a ranked cohort WAS built
    assert card["benchmark"]["jobs_created"]["percentile"] == 81
    # The note must NOT contradict the visible rankings.
    note = (card["data_note"] or "").lower()
    assert "hidden" not in note
    assert "benchmarking isn't available" not in note
    # It should still be transparent that the profile row is backfilled from HubSpot.
    assert "benchmark" in note or "profile" in note


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


# --------------------------------------------------------------------------- #
# (g) benchmark cohort OVERRIDE filters (business_type / size_band / peer_group)
#
# The synthetic _FakeClient ignores the SQL text, so to prove an override actually
# reshapes the cohort WHERE clause we use a capturing client that records the SQL +
# bound parameters while still returning a fixed cohort. We assert on BOTH the emitted
# predicate/params AND the honest `applied_filters` the adapter returns.
# --------------------------------------------------------------------------- #
class _CapturingClient(_FakeClient):
    """Fake Data API client that captures the last Sql + Parameters so a test can assert
    on the cohort predicate / bound params an override produced."""
    def __init__(self, columns, rows, sink):
        super().__init__(columns, rows)
        self._sink = sink

    def execute_statement(self, **k):
        self._sink["sql"] = k.get("Sql", "")
        self._sink["params"] = {p["name"]: p["value"] for p in k.get("Parameters", [])}
        return {"Id": "stmt-1"}


def _wire_capturing(monkeypatch, columns, rows, sink):
    from adapters import sources
    monkeypatch.setattr(sources.AccountPerformance, "live", lambda self: True)
    monkeypatch.setattr(sources.AccountPerformance, "POLL_INTERVAL_S", 0)
    monkeypatch.setattr(sources.Churn, "_target_kwargs", lambda self: {})
    monkeypatch.setattr(sources.Churn, "_identifier",
                        staticmethod(lambda value, qualified=False: value))
    monkeypatch.setattr(sources.Churn, "_client",
                        lambda self: _CapturingClient(columns, rows, sink))


_COHORT_ROWS = [
    ["AU1-TARGET", 100, 200, 40, 800, 10, 10, 12, 200],
    ["AU1-A",       90, 150, 30, 600,  5,  9,  8, 450],
    ["AU1-B",       50,  80, 20, 300,  5,  5,  4, 300],
]


def test_benchmark_default_unchanged_and_reports_applied_filters(monkeypatch):
    """No override params => the cohort is matched on the target's own (icp, account_type)
    exactly as before, and the honest `applied_filters` records the default peer_group."""
    from adapters import sources
    sink = {}
    _wire_capturing(monkeypatch, _BENCH_COLS, _COHORT_ROWS, sink)

    bench = sources.ACCOUNT_PERF.benchmark("AU1-TARGET")
    assert bench["cohort"]["peers"] == 3
    # Default predicate still joins on the target's own icp AND account_type.
    assert "j.icp = t.icp" in sink["sql"]
    assert "j.account_type = t.account_type" in sink["sql"]
    # No override params bound.
    assert set(sink["params"].keys()) == {"ref"}
    af = bench["applied_filters"]
    assert af["peer_group"] == "icp+account_type"
    assert af["business_type"] is None
    assert af["size_band"] is None
    assert af["size_band_ignored"] is None
    assert af["matched_dimensions"] == ["icp", "account_type"]


def test_benchmark_business_type_override_changes_cohort(monkeypatch):
    """A business_type override replaces the target's own account_type match with an explicit
    bound account_type (:bt) — the cohort is reshaped, not the target's auto type."""
    from adapters import sources
    sink = {}
    _wire_capturing(monkeypatch, _BENCH_COLS, _COHORT_ROWS, sink)

    bench = sources.ACCOUNT_PERF.benchmark("AU1-TARGET", business_type="Staffing")
    # The auto account_type match is gone; an explicit bound :bt predicate is used instead.
    assert "j.account_type = :bt" in sink["sql"]
    assert "j.account_type = t.account_type" not in sink["sql"]
    assert sink["params"]["bt"] == "Staffing"
    # icp is still matched (business_type only overrides the account_type dimension).
    assert "j.icp = t.icp" in sink["sql"]
    assert bench["applied_filters"]["business_type"] == "Staffing"


def test_benchmark_size_band_filters_cohort(monkeypatch):
    """A recognised size_band binds a :sb predicate against the derived size_band column."""
    from adapters import sources
    sink = {}
    _wire_capturing(monkeypatch, _BENCH_COLS, _COHORT_ROWS, sink)

    bench = sources.ACCOUNT_PERF.benchmark("AU1-TARGET", size_band="26-75")
    assert "j.size_band = :sb" in sink["sql"]
    assert sink["params"]["sb"] == "26-75"
    af = bench["applied_filters"]
    assert af["size_band"] == "26-75"
    assert af["size_band_ignored"] is None


def test_benchmark_unknown_size_band_is_honestly_ignored(monkeypatch):
    """An unrecognised size_band is a NO-OP (never fabricated): no :sb predicate is bound and
    the honest `applied_filters` records it under size_band_ignored."""
    from adapters import sources
    sink = {}
    _wire_capturing(monkeypatch, _BENCH_COLS, _COHORT_ROWS, sink)

    bench = sources.ACCOUNT_PERF.benchmark("AU1-TARGET", size_band="900-9000")
    assert "j.size_band = :sb" not in sink["sql"]
    assert "sb" not in sink["params"]
    af = bench["applied_filters"]
    assert af["size_band"] is None
    assert af["size_band_ignored"] == "900-9000"


def test_benchmark_peer_group_all_uses_whole_cohort(monkeypatch):
    """peer_group='all' drops the auto (icp, account_type) match => whole live cohort."""
    from adapters import sources
    sink = {}
    _wire_capturing(monkeypatch, _BENCH_COLS, _COHORT_ROWS, sink)

    bench = sources.ACCOUNT_PERF.benchmark("AU1-TARGET", peer_group="all")
    assert "j.icp = t.icp" not in sink["sql"]
    assert "j.account_type = t.account_type" not in sink["sql"]
    assert "WHERE TRUE" in sink["sql"]
    assert bench["applied_filters"]["peer_group"] == "all"
    assert bench["applied_filters"]["matched_dimensions"] == []


def test_benchmark_peer_group_size_matches_target_band(monkeypatch):
    """peer_group='size' matches the target's own size band (j.size_band = t.size_band)."""
    from adapters import sources
    sink = {}
    _wire_capturing(monkeypatch, _BENCH_COLS, _COHORT_ROWS, sink)

    bench = sources.ACCOUNT_PERF.benchmark("AU1-TARGET", peer_group="size")
    assert "j.size_band = t.size_band" in sink["sql"]
    assert bench["applied_filters"]["matched_dimensions"] == ["size_band"]


def test_benchmark_unknown_peer_group_falls_back_to_default(monkeypatch):
    """An unknown peer_group key honestly falls back to the default icp+account_type."""
    from adapters import sources
    sink = {}
    _wire_capturing(monkeypatch, _BENCH_COLS, _COHORT_ROWS, sink)

    bench = sources.ACCOUNT_PERF.benchmark("AU1-TARGET", peer_group="nonsense")
    assert bench["applied_filters"]["peer_group"] == "icp+account_type"
    assert "j.icp = t.icp" in sink["sql"]
    assert "j.account_type = t.account_type" in sink["sql"]


def test_size_band_for_ranges():
    """The Python size-band mapping buckets by the real user count and honestly returns None
    for a missing/zero count (never fabricates a band)."""
    from adapters import sources
    SB = sources.AccountPerformance._size_band_for
    assert SB(1) == "1-25"
    assert SB(25) == "1-25"
    assert SB(26) == "26-75"
    assert SB(75) == "26-75"
    assert SB(76) == "76-100"
    assert SB(100) == "76-100"
    assert SB(101) == "101+"
    assert SB(5000) == "101+"
    assert SB(0) is None
    assert SB(None) is None
    assert SB("") is None


# --------------------------------------------------------------------------- #
# (h) benchmark_filter_options: distinct business types + fixed vocabularies
# --------------------------------------------------------------------------- #
def test_benchmark_filter_options_returns_distinct_values(monkeypatch):
    from adapters import sources
    # The distinct-account_type query returns one column 'account_type' with rows.
    rows = [["Staffing"], ["Agency"], ["Corporate"]]
    _wire_acct_perf(monkeypatch, ["account_type"], rows)

    opts = sources.ACCOUNT_PERF.benchmark_filter_options()
    assert opts["business_types"] == ["Staffing", "Agency", "Corporate"]
    # size_bands / peer_groups are the fixed derived vocabularies.
    assert opts["size_bands"] == ["1-25", "26-75", "76-100", "101+"]
    assert "icp+account_type" in opts["peer_groups"]
    assert "all" in opts["peer_groups"]
    assert opts["_source"] == "redshift-live"


def test_benchmark_filter_options_empty_when_not_live(monkeypatch):
    from adapters import sources
    monkeypatch.setattr(sources.AccountPerformance, "live", lambda self: False)
    assert sources.ACCOUNT_PERF.benchmark_filter_options() == {}


# --------------------------------------------------------------------------- #
# (i) engine threads the filters and returns the benchmark_filters contract
# --------------------------------------------------------------------------- #
def test_engine_threads_benchmark_filters_and_returns_contract(monkeypatch):
    """engine.account_performance passes business_type/size_band/peer_group to the adapter
    and returns a benchmark_filters block with applied/requested/options for the UI."""
    import engine, dataaccess
    from adapters import sources
    monkeypatch.setattr(dataaccess, "all_accounts", _fake_accounts)
    monkeypatch.setattr(engine, "can_view_account", lambda account_id: True)

    captured = {}

    def _fake_benchmark(self, ref, business_type=None, size_band=None, peer_group=None):
        captured["args"] = (business_type, size_band, peer_group)
        return {"_source": "redshift-live", "cohort": {"peers": 3},
                "applied_filters": {"peer_group": peer_group or "icp+account_type",
                                    "business_type": business_type, "size_band": size_band,
                                    "size_band_ignored": None,
                                    "matched_dimensions": ["icp"]}}

    monkeypatch.setattr(sources.AccountPerformance, "live", lambda self: True)
    monkeypatch.setattr(sources.AccountPerformance, "dimension", lambda self, ref: {})
    monkeypatch.setattr(sources.AccountPerformance, "performance", lambda self, ref: {})
    monkeypatch.setattr(sources.AccountPerformance, "benchmark", _fake_benchmark)
    monkeypatch.setattr(sources.AccountPerformance, "feature_usage", lambda self, ref: {})
    monkeypatch.setattr(sources.AccountPerformance, "benchmark_filter_options",
                        lambda self: {"business_types": ["Agency", "Staffing"],
                                      "size_bands": ["1-25", "26-75", "76-100", "101+"],
                                      "peer_groups": ["icp+account_type", "all"],
                                      "_source": "redshift-live"})
    monkeypatch.setattr(sources.HubSpot, "live", lambda self: False)
    monkeypatch.setattr(sources.Zendesk, "live", lambda self: False)
    monkeypatch.setattr(sources.AccountMetrics, "live", lambda self: False)
    monkeypatch.setattr(engine, "full_roster", lambda: {"companies": [
        {"account_id": "au1-1", "name": "Alpha"}]})

    engine.set_principal(None)
    card = engine.account_performance("au1-1", business_type="Agency",
                                      size_band="26-75", peer_group="all")
    # The adapter received exactly the threaded params.
    assert captured["args"] == ("Agency", "26-75", "all")

    bf = card["benchmark_filters"]
    assert bf["requested"] == {"business_type": "Agency", "size_band": "26-75", "peer_group": "all"}
    assert bf["applied"]["business_type"] == "Agency"
    assert bf["applied"]["size_band"] == "26-75"
    assert bf["options"]["business_types"] == ["Agency", "Staffing"]
    assert bf["options"]["size_bands"] == ["1-25", "26-75", "76-100", "101+"]


def test_engine_default_call_signature_backward_compatible(monkeypatch):
    """Calling engine.account_performance(account_id) with no filter kwargs still works and
    yields an empty applied-filters/options when the warehouse is offline (honest, no crash)."""
    import engine, dataaccess
    from adapters import sources
    monkeypatch.setattr(dataaccess, "all_accounts", _fake_accounts)
    monkeypatch.setattr(engine, "can_view_account", lambda account_id: True)
    _offline_all_sources(monkeypatch)
    monkeypatch.setattr(sources.AccountMetrics, "live", lambda self: False)
    monkeypatch.setattr(engine, "full_roster", lambda: {"companies": [
        {"account_id": "au1-1", "name": "Alpha"}]})

    engine.set_principal(None)
    card = engine.account_performance("au1-1")
    bf = card["benchmark_filters"]
    assert bf["requested"] == {"business_type": None, "size_band": None, "peer_group": None}
    assert bf["applied"] == {}
    assert bf["options"] == {}


def test_engine_filter_options_helper_offline_is_empty(monkeypatch):
    """account_performance_filter_options() is honestly {} when the warehouse is not live."""
    import engine
    from adapters import sources
    monkeypatch.setattr(sources.AccountPerformance, "live", lambda self: False)
    assert engine.account_performance_filter_options() == {}


def test_engine_filter_options_helper_live_returns_options(monkeypatch):
    import engine
    from adapters import sources
    monkeypatch.setattr(sources.AccountPerformance, "live", lambda self: True)
    monkeypatch.setattr(sources.AccountPerformance, "benchmark_filter_options",
                        lambda self: {"business_types": ["Agency"], "size_bands": ["1-25"],
                                      "peer_groups": ["all"], "_source": "redshift-live"})
    opts = engine.account_performance_filter_options()
    assert opts["business_types"] == ["Agency"]
    assert opts["peer_groups"] == ["all"]

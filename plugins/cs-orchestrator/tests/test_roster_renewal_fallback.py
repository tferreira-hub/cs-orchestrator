"""Whole-book roster renewal fallback.

Regression test for the renewal-coverage gap found against live HubSpot: the renewal
truth lives on the closed-won DEAL, not on the company object (only ~75 of ~4,300
active customers carry any company-level renewal property). The deep per-account path
already derived renewal from the deal; the whole-book roster path did not, so the
Renewals-by-Month / Upcoming Renewals views showed ~75 rows instead of the whole book.

These tests pin:
  * the shared derivation helpers (date math + closed-won selection) so the roster and
    the deep per-account path can never disagree;
  * _fill_roster_renewals_from_deals: batched (association + deal batch/read), fills
    renewal_date + renewal_source='deal' for company-blank rows, and NEVER invents a
    value when the deal has no derivable term.

Run:  python3 -m pytest tests/test_roster_renewal_fallback.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN))

from adapters import config, sources  # noqa: E402


# --------------------------------------------------------------------------- #
# Shared derivation helpers (roster path and deep path use the SAME code)
# --------------------------------------------------------------------------- #
def test_derive_renewal_adds_contract_term_to_signed_date():
    h = sources.HubSpot()
    out = h._derive_renewal_from_deal_props(
        {"deal_signed_date": "2024-03-15", "contract_length__months_": "12"})
    assert out == "2025-03-15"


def test_derive_renewal_falls_back_to_closedate():
    h = sources.HubSpot()
    out = h._derive_renewal_from_deal_props(
        {"closedate": "2024-01-31T00:00:00Z", "contract_length__months_": "24"})
    # +24 months, day clamped to 28 to stay valid across month lengths.
    assert out == "2026-01-28"


def test_derive_renewal_none_without_term():
    h = sources.HubSpot()
    assert h._derive_renewal_from_deal_props({"closedate": "2024-01-01"}) is None
    assert h._derive_renewal_from_deal_props({"contract_length__months_": "12"}) is None
    assert h._derive_renewal_from_deal_props({}) is None


def test_pick_prefers_closed_won_then_newest():
    h = sources.HubSpot()
    deals = [
        {"hs_is_closed_won": "false", "closedate": "2025-01-01"},
        {"hs_is_closed_won": "true", "closedate": "2023-06-01"},
        {"hs_is_closed_won": "true", "closedate": "2024-09-01"},
    ]
    picked = h._pick_closed_won_deal(deals)
    # Closed-won wins over the newer open deal; among won, the newest closedate.
    assert picked["closedate"] == "2024-09-01"


def test_pick_returns_none_on_empty():
    h = sources.HubSpot()
    assert h._pick_closed_won_deal([]) is None


# --------------------------------------------------------------------------- #
# Batched roster fallback
# --------------------------------------------------------------------------- #
class _FakeHubSpot(sources.HubSpot):
    """HubSpot adapter with HTTP stubbed to a scripted association + deal response."""

    def _headers(self):
        return {"Authorization": "Bearer test"}


def _install_http(monkeypatch, assoc_results, deal_results):
    """Stub config.http_post so the association-batch-read returns `assoc_results` and
    the deals batch/read returns `deal_results`. Fails the test on any other URL."""
    def fake_post(url, headers, body, timeout=12):
        if "associations/companies/deals/batch/read" in url:
            return {"results": assoc_results}
        if "objects/deals/batch/read" in url:
            return {"results": deal_results}
        raise AssertionError(f"unexpected POST to {url}")
    monkeypatch.setattr(config, "http_post", fake_post)


def test_fill_fills_company_blank_rows_from_deal(monkeypatch):
    h = _FakeHubSpot()
    rows = [
        {"company_id": "1", "name": "Blank Co", "renewal_date": None, "renewal_source": None},
        {"company_id": "2", "name": "Has Company Renewal", "renewal_date": "2026-05-01",
         "renewal_source": "company"},
    ]
    assoc = [{"from": {"id": "1"}, "to": [{"toObjectId": "900"}]}]
    deals = [{"id": "900", "properties": {
        "hs_is_closed_won": "true", "closedate": "2024-02-10",
        "deal_signed_date": "2024-02-10", "contract_length__months_": "12"}}]
    _install_http(monkeypatch, assoc, deals)

    filled = h._fill_roster_renewals_from_deals(rows)

    assert filled == 1
    assert rows[0]["renewal_date"] == "2025-02-10"
    assert rows[0]["renewal_source"] == "deal"
    # The row that already had a company renewal is untouched.
    assert rows[1]["renewal_date"] == "2026-05-01"
    assert rows[1]["renewal_source"] == "company"


def test_fill_never_invents_when_no_derivable_term(monkeypatch):
    """Honest gap: a deal with no contract term leaves renewal_date None (never faked)."""
    h = _FakeHubSpot()
    rows = [{"company_id": "1", "name": "No Term Co", "renewal_date": None,
             "renewal_source": None}]
    assoc = [{"from": {"id": "1"}, "to": [{"toObjectId": "900"}]}]
    deals = [{"id": "900", "properties": {"hs_is_closed_won": "true",
                                          "closedate": "2024-02-10"}}]  # no term
    _install_http(monkeypatch, assoc, deals)

    filled = h._fill_roster_renewals_from_deals(rows)

    assert filled == 0
    assert rows[0]["renewal_date"] is None
    assert rows[0]["renewal_source"] is None


def test_fill_no_targets_makes_no_http_calls(monkeypatch):
    """If every row already has a renewal, the fallback must not call HubSpot at all."""
    h = _FakeHubSpot()
    rows = [{"company_id": "1", "renewal_date": "2026-05-01", "renewal_source": "company"}]

    def boom(*a, **k):
        raise AssertionError("http_post must not be called when there are no targets")
    monkeypatch.setattr(config, "http_post", boom)

    assert h._fill_roster_renewals_from_deals(rows) == 0


def test_fill_picks_closed_won_among_multiple_deals(monkeypatch):
    h = _FakeHubSpot()
    rows = [{"company_id": "7", "renewal_date": None, "renewal_source": None}]
    assoc = [{"from": {"id": "7"}, "to": [{"toObjectId": "10"}, {"toObjectId": "11"}]}]
    deals = [
        {"id": "10", "properties": {"hs_is_closed_won": "false", "closedate": "2025-01-01",
                                    "deal_signed_date": "2025-01-01",
                                    "contract_length__months_": "12"}},
        {"id": "11", "properties": {"hs_is_closed_won": "true", "closedate": "2024-03-15",
                                    "deal_signed_date": "2024-03-15",
                                    "contract_length__months_": "12"}},
    ]
    _install_http(monkeypatch, assoc, deals)

    h._fill_roster_renewals_from_deals(rows)

    # The closed-won deal (id 11) is used, not the newer open one (id 10).
    assert rows[0]["renewal_date"] == "2025-03-15"
    assert rows[0]["renewal_source"] == "deal"

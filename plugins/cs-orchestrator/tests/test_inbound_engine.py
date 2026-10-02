"""Tests for the Scaled Tech-Touch inbound engine (triage + round-robin + dedupe).

Pure, deterministic — no network. Verifies the Use Case 1 logic from Tech Touch V3:
intent classification + routing, round-robin across available CSMs, 24h SLA / 20h
reassign, and 2-hour duplicate merging.
"""

from __future__ import annotations

import sys
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN))

import inbound  # noqa: E402

NOW = 1_700_000_000


def _item(id, frm, subject, body="", channel="mailbox", received=NOW, account_ref=None):
    return {"id": id, "from": frm, "subject": subject, "body": body,
            "channel": channel, "received_at": received, "account_ref": account_ref}


# --------------------------------------------------------------------------- #
# Intent classification + routing
# --------------------------------------------------------------------------- #
def test_classify_technical_routes_to_zendesk_handoff():
    assert inbound.classify_intent("the app keeps throwing an error on login") == "technical"
    r = inbound.triage_inbound([_item("1", "a@co.com", "Bug", "there is a crash")],
                               roster=[{"name": "Casey", "available": True}], now=NOW)
    t = r["tickets"][0]
    assert t["intent"] == "technical"
    assert t["destination"] == "zendesk_handoff"
    # Technical items leave the CS queue -> not assigned to a pooled CSM.
    assert t["assigned_to"] is None


def test_classify_billing_routes_to_pooled_queue():
    assert inbound.classify_intent("question about my invoice") == "billing"
    r = inbound.triage_inbound([_item("1", "a@co.com", "Invoice query", "my invoice is wrong")],
                               roster=[{"name": "Casey", "available": True}], now=NOW)
    t = r["tickets"][0]
    assert t["intent"] == "billing" and t["destination"] == "cs_pooled_queue"
    assert t["assigned_to"] == "Casey"
    assert t["csql"] is False


def test_classify_expansion_is_high_priority_csql():
    assert inbound.classify_intent("we want to add seats / upgrade our licence") == "expansion"
    r = inbound.triage_inbound([_item("1", "a@co.com", "Upgrade", "please add 10 licence seats")],
                               roster=[{"name": "Casey", "available": True}], now=NOW)
    t = r["tickets"][0]
    assert t["intent"] == "expansion" and t["destination"] == "expansion_queue"
    assert t["csql"] is True and t["priority"] == 2


def test_expansion_wins_over_technical_tie():
    # Mentions both an upgrade and an error: expansion (commercial value) takes precedence.
    assert inbound.classify_intent("want to upgrade our licence but we also hit an error") == "expansion"


def test_general_when_no_keywords():
    assert inbound.classify_intent("hello, just checking in") == "general"


# --------------------------------------------------------------------------- #
# Round-robin + availability
# --------------------------------------------------------------------------- #
def test_round_robin_even_distribution_across_available():
    roster = [{"name": "Ann", "available": True},
              {"name": "Bob", "available": True},
              {"name": "Cat", "available": True}]
    items = [_item(str(i), f"user{i}@co.com", "Invoice", "invoice query", received=NOW + i)
             for i in range(6)]
    r = inbound.triage_inbound(items, roster=roster, now=NOW + 100)
    load = r["summary"]["load_per_csm"]
    # 6 billing tickets across 3 available CSMs -> 2 each.
    assert load == {"Ann": 2, "Bob": 2, "Cat": 2}


def test_unavailable_csm_is_skipped():
    roster = [{"name": "Ann", "available": False},
              {"name": "Bob", "available": True}]
    items = [_item(str(i), f"user{i}@co.com", "Invoice", "billing", received=NOW + i)
             for i in range(4)]
    r = inbound.triage_inbound(items, roster=roster, now=NOW + 100)
    load = r["summary"]["load_per_csm"]
    assert load == {"Bob": 4}
    assert r["summary"]["available_csms"] == ["Bob"]


def test_no_available_csm_leaves_unassigned():
    roster = [{"name": "Ann", "available": False}]
    r = inbound.triage_inbound([_item("1", "a@co.com", "Invoice", "billing")],
                               roster=roster, now=NOW)
    assert r["tickets"][0]["assigned_to"] is None


# --------------------------------------------------------------------------- #
# SLA + reassignment
# --------------------------------------------------------------------------- #
def test_sla_due_is_24h_from_receipt():
    r = inbound.triage_inbound([_item("1", "a@co.com", "Invoice", "billing", received=NOW)],
                               roster=[{"name": "Ann", "available": True}], now=NOW)
    assert r["tickets"][0]["sla_due"] == NOW + 24 * 3600
    assert r["tickets"][0]["sla_breached"] is False


def test_breached_when_past_due():
    old = NOW - 25 * 3600
    r = inbound.triage_inbound([_item("1", "a@co.com", "Invoice", "billing", received=old)],
                               roster=[{"name": "Ann", "available": True}], now=NOW)
    assert r["tickets"][0]["sla_breached"] is True


def test_needs_reassign_after_20h():
    old = NOW - 21 * 3600  # 21h old, not yet breached, assigned -> reassign
    r = inbound.triage_inbound([_item("1", "a@co.com", "Invoice", "billing", received=old)],
                               roster=[{"name": "Ann", "available": True}], now=NOW)
    t = r["tickets"][0]
    assert t["sla_breached"] is False
    assert t["needs_reassign"] is True


# --------------------------------------------------------------------------- #
# Duplicate merging (2h window, same sender)
# --------------------------------------------------------------------------- #
def test_duplicate_merge_within_2h_window():
    items = [
        _item("a", "same@co.com", "Invoice", "first message", received=NOW),
        _item("b", "same@co.com", "Invoice", "same thing again", received=NOW + 1800),  # +30m
    ]
    r = inbound.triage_inbound(items, roster=[{"name": "Ann", "available": True}], now=NOW + 3600)
    assert r["summary"]["tickets"] == 1
    assert r["summary"]["merged"] == 1
    assert r["tickets"][0]["merged_ids"] == ["b"]


def test_messages_outside_2h_window_are_separate():
    items = [
        _item("a", "same@co.com", "Invoice", "first", received=NOW),
        _item("b", "same@co.com", "Invoice", "much later", received=NOW + 3 * 3600),  # +3h
    ]
    r = inbound.triage_inbound(items, roster=[{"name": "Ann", "available": True}], now=NOW + 4 * 3600)
    assert r["summary"]["tickets"] == 2
    assert r["summary"]["merged"] == 0


def test_summary_counts_are_consistent():
    roster = [{"name": "Ann", "available": True}, {"name": "Bob", "available": True}]
    items = [
        _item("1", "u1@co.com", "Bug", "crash error"),           # technical
        _item("2", "u2@co.com", "Invoice", "invoice question"),  # billing
        _item("3", "u3@co.com", "Upgrade", "add seats please"),  # expansion
        _item("4", "u4@co.com", "Hello", "just saying hi"),      # general
    ]
    r = inbound.triage_inbound(items, roster=roster, now=NOW)
    s = r["summary"]
    assert s["received"] == 4 and s["tickets"] == 4
    assert s["by_intent"] == {"expansion": 1, "technical": 1, "billing": 1, "general": 1}
    assert s["zendesk_handoffs"] == 1 and s["csqls"] == 1
    # Technical not assigned; the other 3 distributed across 2 CSMs.
    assert sum(s["load_per_csm"].values()) == 3

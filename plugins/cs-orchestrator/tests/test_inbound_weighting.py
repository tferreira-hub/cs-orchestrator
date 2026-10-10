"""Per-channel inbound prioritisation weighting (blend model).

The platform IS the help desk — the five inbound paths map straight in — and each path
carries a prioritisation weight that BLENDS with intent: intent sets the band (a genuine
fault still beats a web form), and the channel weight orders within/near the band so the
SOURCE influences prioritisation (a high-intent form outranks a generic mailbox email at
the same intent level). Lower priority_score = more urgent.
"""
from __future__ import annotations

import sys
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN))

import inbound  # noqa: E402


def test_default_channel_weights_ranked():
    w = {c: inbound.channel_weight(c) for c in
         ("high_intent_form", "slack_call", "zendesk_misroute", "campaign_reply", "mailbox")}
    # The agreed default ranking (highest = ranks soonest).
    assert w["high_intent_form"] > w["slack_call"] > w["zendesk_misroute"] > w["campaign_reply"] >= w["mailbox"]
    assert all(0.0 <= v <= 1.0 for v in w.values())


def test_unknown_channel_gets_default_weight():
    assert inbound.channel_weight("something_new") == inbound.DEFAULT_CHANNEL_WEIGHT


def test_env_override_and_clamp(monkeypatch):
    monkeypatch.setenv("CS_CHANNEL_WEIGHTS", '{"mailbox": 0.95, "high_intent_form": 5}')
    assert inbound.channel_weight("mailbox") == 0.95       # overridden
    assert inbound.channel_weight("high_intent_form") == 1.0  # clamped to [0,1]
    assert inbound.channel_weight("slack_call") == 0.8     # untouched default


def test_blend_score_lower_is_more_urgent():
    # Same intent band (P3); the higher-weight channel scores lower (more urgent).
    zd = inbound.effective_priority_score(3, "zendesk_misroute")   # 3 - 0.6 = 2.4
    mb = inbound.effective_priority_score(3, "mailbox")            # 3 - 0.3 = 2.7
    assert zd < mb


def test_fault_still_beats_a_lower_band_form():
    """A genuine technical fault (P3) must NOT be leapfrogged by a high-weight form at a
    WORSE intent band — the channel swing (<1) never crosses a full priority band. But an
    expansion form (P2) legitimately outranks a P3 fault."""
    fault_p3 = inbound.effective_priority_score(3, "mailbox")          # 2.7
    form_general_p4 = inbound.effective_priority_score(4, "high_intent_form")  # 3.0
    assert fault_p3 < form_general_p4   # the P3 fault still ranks above a P4 form
    form_expansion_p2 = inbound.effective_priority_score(2, "high_intent_form")  # 1.0
    assert form_expansion_p2 < fault_p3  # a real expansion form (P2) outranks the P3 fault


def test_triage_tickets_carry_weight_and_sort_by_blend():
    items = [
        {"id": "m1", "channel": "mailbox", "from": "a@x.com", "subject": "invoice",
         "body": "billing question", "received_at": 1000},
        {"id": "f1", "channel": "high_intent_form", "from": "b@x.com", "subject": "upgrade",
         "body": "add 10 licence seats", "received_at": 1000},
        {"id": "z1", "channel": "zendesk_misroute", "from": "c@x.com", "subject": "down",
         "body": "system is down crash", "received_at": 1000},
    ]
    out = inbound.triage_inbound(items, roster=[{"name": "A", "available": True}], now=2000)
    tickets = out["tickets"]
    # Each ticket carries its channel weight + blended score.
    for t in tickets:
        assert isinstance(t["channel_weight"], float)
        assert isinstance(t["priority_score"], float)
    # Sorted most-urgent first: expansion form (1.0) < zendesk fault (2.4) < mailbox billing (2.7).
    assert [t["id"] for t in tickets] == ["f1", "z1", "m1"]

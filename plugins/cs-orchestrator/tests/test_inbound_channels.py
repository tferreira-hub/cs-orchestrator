"""Tests for the 5 inbound channel adapters (Tech Touch V3, UC1).

Pure and deterministic - the normalisers do no I/O. Verifies each native payload shape maps
to the inbound-item shape triage_inbound() consumes, that the resulting items classify to
the right intent/route, timestamps coerce, and that an unknown channel / unparseable payload
is an honest no-op (never fabricated).
"""

from __future__ import annotations

import sys
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN))

import inbound  # noqa: E402


def _one(channel, payload):
    items = inbound.normalise_channel(channel, payload)
    assert len(items) == 1, f"{channel}: expected 1 item, got {items}"
    it = items[0]
    assert it["channel"] == channel
    return it


# --------------------------------------------------------------------------- #
# Channel 1: Zendesk misroute
# --------------------------------------------------------------------------- #
def test_zendesk_misroute_normalises_and_classifies_billing():
    it = _one("zendesk_misroute", {"ticket": {
        "id": 55, "subject": "Invoice question", "description": "my subscription invoice is wrong",
        "requester": {"email": "ops@acme.com"}, "organization_name": "Acme",
        "created_at": "2026-10-01T09:00:00Z"}})
    assert it["from"] == "ops@acme.com"
    assert it["subject"] == "Invoice question"
    assert it["account_name"] == "Acme"
    assert isinstance(it["received_at"], int) and it["received_at"] > 0
    assert inbound.classify_intent(it["subject"] + " " + it["body"]) == "billing"


# --------------------------------------------------------------------------- #
# Channel 2: Slack call log
# --------------------------------------------------------------------------- #
def test_slack_call_keeps_request_type_in_text():
    it = _one("slack_call", {"fields": {
        "email": "caller@beta.com", "request_type": "billing", "brief": "wants a receipt copy",
        "company": "Beta Ltd"}})
    assert it["from"] == "caller@beta.com"
    assert "billing" in it["body"].lower()
    assert it["account_name"] == "Beta Ltd"
    assert inbound.classify_intent(it["subject"] + " " + it["body"]) == "billing"


# --------------------------------------------------------------------------- #
# Channel 3: Mailbox
# --------------------------------------------------------------------------- #
def test_mailbox_technical_routes_zendesk():
    it = _one("mailbox", {"from": "u@gamma.com", "subject": "App is down",
                          "text": "the whole thing is down, we see a 500 error",
                          "message_id": "m-1", "date": 1_700_000_000})
    r = inbound.triage_inbound([it], roster=[{"name": "Casey", "available": True}], now=1_700_000_100)
    t = r["tickets"][0]
    assert t["intent"] == "technical" and t["destination"] == "zendesk_handoff"
    assert t["assigned_to"] is None  # technical leaves the pooled queue


# --------------------------------------------------------------------------- #
# Channel 4: Campaign reply
# --------------------------------------------------------------------------- #
def test_campaign_reply_carries_account_ref():
    it = _one("campaign_reply", {"message": {
        "from": "champion@delta.com", "subject": "Re: Your renewal", "text": "yes let's talk",
        "account_ref": "AU1-1234", "message_id": "c-9"}})
    assert it["account_ref"] == "AU1-1234"
    assert it["channel"] == "campaign_reply"


# --------------------------------------------------------------------------- #
# Channel 5: High-intent form -> expansion / CSQL
# --------------------------------------------------------------------------- #
def test_high_intent_form_classifies_expansion_csql():
    it = _one("high_intent_form", {"fields": {
        "email": "buyer@epsilon.com", "company": "Epsilon",
        "request": "we want to add 10 users", "form_name": "Add licences"}})
    r = inbound.triage_inbound([it], roster=[{"name": "Casey", "available": True}], now=1_700_000_000)
    t = r["tickets"][0]
    assert t["intent"] == "expansion"
    assert t["csql"] is True
    assert t["destination"] == "expansion_queue"


# --------------------------------------------------------------------------- #
# Timestamp coercion
# --------------------------------------------------------------------------- #
def test_epoch_coercion_iso_seconds_and_millis():
    assert inbound._epoch("2026-10-01T00:00:00Z") == 1790812800
    assert inbound._epoch(1_700_000_000) == 1_700_000_000
    assert inbound._epoch(1_700_000_000_000) == 1_700_000_000  # millis -> seconds
    assert inbound._epoch("") is None
    assert inbound._epoch("garbage") is None


# --------------------------------------------------------------------------- #
# Honesty: unknown channel / unparseable payload -> no items (never fabricated)
# --------------------------------------------------------------------------- #
def test_unknown_channel_and_empty_payload_are_noops():
    assert inbound.normalise_channel("telepathy", {"subject": "hi"}) == []
    assert inbound.normalise_channel("mailbox", {}) == []          # no subject/body
    assert inbound.normalise_channel("mailbox", "not a dict") == []
    assert inbound.normalise_channel("zendesk_misroute", {"ticket": {}}) == []


def test_dispatch_table_covers_the_five_channels():
    assert set(inbound.CHANNEL_ADAPTERS) == {
        "zendesk_misroute", "slack_call", "mailbox", "campaign_reply", "high_intent_form"}

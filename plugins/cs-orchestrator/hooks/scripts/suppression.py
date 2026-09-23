#!/usr/bin/env python3
"""Multi-instance suppression, CS WoW §5 "prevent false-positive alerts".

Deterministic enforcement used by the harness: signals originating on a
`secondary` or `test` instance must NOT drive risk/expansion alerts; only the
PRIMARY / high-ARR instance counts. This is the harness "enforcement" primitive,
it cannot be bypassed by the model's judgement.

Pure functions so they are unit-testable and reusable by the dry-run driver and
the grounding gate.
"""

from __future__ import annotations

from typing import Any

PRIMARY_TYPES = {"primary"}


def primary_instance_ids(hubspot: dict[str, Any]) -> set[str]:
    """Instance ids considered authoritative for alerting (primary only)."""
    return {
        inst["instance_id"]
        for inst in hubspot.get("instances", [])
        if inst.get("instance_type") in PRIMARY_TYPES
    }


def non_primary_instance_ids(hubspot: dict[str, Any]) -> set[str]:
    return {
        inst["instance_id"]
        for inst in hubspot.get("instances", [])
        if inst.get("instance_type") not in PRIMARY_TYPES
    }


def ticket_spike_on_primary(zendesk: dict[str, Any], primary_ids: set[str]) -> tuple[bool, dict[str, Any]]:
    """A ticket spike counts only if it is concentrated on a primary instance.

    Returns (fired, evidence). If most of the recent tickets are on non-primary
    instances, the spike is suppressed as noise.
    """
    last7 = zendesk.get("tickets_last_7d", 0)
    prev7 = zendesk.get("tickets_prev_7d", 0)
    by_instance = zendesk.get("by_instance", {})
    primary_tickets = sum(v for k, v in by_instance.items() if k in primary_ids)
    non_primary_tickets = sum(v for k, v in by_instance.items() if k not in primary_ids)

    raw_spike = prev7 > 0 and last7 >= 2 * prev7
    # Suppress if the spike is driven by non-primary instances.
    primary_driven = primary_tickets > non_primary_tickets
    fired = raw_spike and primary_driven
    evidence = {
        "tickets_last_7d": last7,
        "tickets_prev_7d": prev7,
        "primary_tickets": primary_tickets,
        "non_primary_tickets": non_primary_tickets,
        "raw_spike": raw_spike,
        "primary_driven": primary_driven,
    }
    return fired, evidence


def suppressed_signals(hubspot: dict[str, Any], zendesk: dict[str, Any]) -> list[dict[str, Any]]:
    """Return a list of signals that WOULD have alerted but are suppressed as
    non-primary-instance noise (for the demo's Suppressed section)."""
    out: list[dict[str, Any]] = []
    primary_ids = primary_instance_ids(hubspot)
    non_primary = non_primary_instance_ids(hubspot)
    by_instance = zendesk.get("by_instance", {})

    last7 = zendesk.get("tickets_last_7d", 0)
    prev7 = zendesk.get("tickets_prev_7d", 0)
    raw_spike = prev7 > 0 and last7 >= 2 * prev7
    if raw_spike:
        non_primary_tickets = sum(v for k, v in by_instance.items() if k in non_primary)
        primary_tickets = sum(v for k, v in by_instance.items() if k in primary_ids)
        if non_primary_tickets >= primary_tickets:
            out.append({
                "signal": "support_ticket_spike",
                "reason": "spike concentrated on non-primary instance(s)",
                "non_primary_instances": sorted(non_primary),
                "non_primary_tickets": non_primary_tickets,
                "primary_tickets": primary_tickets,
            })
    return out


if __name__ == "__main__":  # tiny self-check
    hs = {"instances": [
        {"instance_id": "p", "instance_type": "primary"},
        {"instance_id": "d", "instance_type": "test"},
    ]}
    zd = {"tickets_last_7d": 2, "tickets_prev_7d": 30, "by_instance": {"p": 1, "d": 4}}
    # ticket volume dropped, so no spike; ensure no crash
    print("primary:", primary_instance_ids(hs))
    print("suppressed:", suppressed_signals(hs, zd))

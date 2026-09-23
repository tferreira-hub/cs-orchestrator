#!/usr/bin/env python3
"""CS Orchestrator, MCP server exposing the Customer Success stack as agent tools.

This is a stdio JSON-RPC 2.0 MCP server (protocol subset: initialize, tools/list,
tools/call). Each tool returns JSON shaped to mirror the real vendor API so the
server is a drop-in swap for live integrations (HubSpot company object, Zendesk
tickets/CSAT, Stripe invoices, product usage telemetry, ML churn score).

Data source: a fixtures JSON file (env CS_FIXTURES), no external calls, so the
demo is deterministic and offline-safe. Swap the _load()/_account() internals for
real API clients to go live.

Tools:
  list_accounts()                 -> [{account_id, name, segment, arr_usd, renewal_date}]
  hubspot_get_account(account_id) -> company object incl. contacts + instances
  zendesk_get_tickets(account_id) -> ticket volume, CSAT, sev1, per-instance
  usage_get_metrics(account_id)   -> logins, utilization, feature adoption, per-instance
  churn_get_score(account_id)     -> ML churn score + drivers
  stripe_get_payment(account_id)  -> past-due invoices, days_past_due, dunning stage
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

# Live adapters (env-keyed) with fixture fallback. Import is optional so the server
# still runs if the adapters package is absent.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
try:
    from adapters import with_fallback
    from adapters import sources as _src
    _ADAPTERS = True
except Exception:  # noqa: BLE001
    _ADAPTERS = False
    def with_fallback(is_live, live_call, fixture_call):  # type: ignore
        return fixture_call()


FIXTURES_PATH = os.environ.get(
    "CS_FIXTURES",
    str(Path(__file__).parent / "fixtures" / "accounts.json"),
)


def _load() -> dict[str, Any]:
    with open(FIXTURES_PATH, encoding="utf-8") as fh:
        return json.load(fh).get("accounts", {})


def _account(account_id: str) -> dict[str, Any]:
    accounts = _load()
    if account_id not in accounts:
        raise KeyError(f"Unknown account_id '{account_id}'. Known: {', '.join(sorted(accounts))}")
    return accounts[account_id]


# --- Tool implementations ---------------------------------------------------

def tool_list_accounts(_: dict[str, Any]) -> Any:
    out = []
    for aid, a in _load().items():
        hs = a.get("hubspot", {})
        out.append({
            "account_id": aid,
            "name": hs.get("name"),
            "segment": hs.get("segment"),
            "arr_usd": hs.get("arr_usd"),
            "renewal_date": hs.get("renewal_date"),
        })
    return out


def tool_hubspot_get_account(args: dict[str, Any]) -> Any:
    aid = args["account_id"]
    fixture = lambda: _account(aid).get("hubspot", {})
    if not _ADAPTERS:
        return fixture()
    return with_fallback(_src.HUBSPOT.live(), lambda: _src.HUBSPOT.account(aid), fixture)


def tool_zendesk_get_tickets(args: dict[str, Any]) -> Any:
    aid = args["account_id"]
    fixture = lambda: _account(aid).get("zendesk", {})
    if not _ADAPTERS:
        return fixture()
    return with_fallback(_src.ZENDESK.live(), lambda: _src.ZENDESK.tickets(aid), fixture)


def tool_usage_get_metrics(args: dict[str, Any]) -> Any:
    aid = args["account_id"]
    fixture = lambda: _account(aid).get("usage", {})
    if not _ADAPTERS:
        return fixture()
    return with_fallback(_src.PENDO.live(), lambda: _src.PENDO.metrics(aid), fixture)


def tool_churn_get_score(args: dict[str, Any]) -> Any:
    return _account(args["account_id"]).get("churn", {})


def tool_stripe_get_payment(args: dict[str, Any]) -> Any:
    aid = args["account_id"]
    fixture = lambda: _account(aid).get("stripe", {})
    if not _ADAPTERS:
        return fixture()
    return with_fallback(_src.STRIPE.live(), lambda: _src.STRIPE.payment(aid), fixture)


def tool_jiminny_get_calls(args: dict[str, Any]) -> Any:
    """Conversational intelligence: latest call sentiment, summary, talk ratio."""
    aid = args["account_id"]
    fixture = lambda: _account(aid).get("jiminny", {})
    if not _ADAPTERS:
        return fixture()
    return with_fallback(_src.JIMINNY.live(), lambda: _src.JIMINNY.calls(aid), fixture)


def tool_hubspot_push_cs_data(args: dict[str, Any]) -> Any:
    """Bi-directional sync: push CS-owned fields (health score, risk status, active
    playbook) BACK to the HubSpot company object so Sales has visibility (req §1).

    Live when HUBSPOT_TOKEN is set; otherwise simulated (fixture mode)."""
    account_id = args["account_id"]
    fields = {
        "health_score": args.get("health_score"),
        "risk_status": args.get("risk_status"),
        "active_playbook": args.get("active_playbook"),
    }

    def _sim():
        _account(account_id)
        return {
            "synced": True,
            "target": "hubspot.crm.companies",
            "account_id": account_id,
            "written_fields": {f"cs_{k}": v for k, v in fields.items() if v is not None},
            "note": "Simulated bi-directional write-back (fixture mode). Set HUBSPOT_TOKEN to write live.",
        }

    if not _ADAPTERS:
        return _sim()
    return with_fallback(
        _src.HUBSPOT.live(),
        lambda: _src.HUBSPOT.push_cs_data(account_id, **fields),
        _sim,
    )


_ACCOUNT_ARG = {
    "type": "object",
    "properties": {"account_id": {"type": "string", "description": "Account id, e.g. acct_northwind"}},
    "required": ["account_id"],
}

TOOLS: dict[str, dict[str, Any]] = {
    "list_accounts": {
        "handler": tool_list_accounts,
        "description": "List all CS accounts with segment, ARR, and renewal date.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    "hubspot_get_account": {
        "handler": tool_hubspot_get_account,
        "description": "HubSpot company object: segment, ARR, renewal date, tagged contacts (Exec Sponsor/Champion/Finance), and instance hierarchy.",
        "inputSchema": _ACCOUNT_ARG,
    },
    "zendesk_get_tickets": {
        "handler": tool_zendesk_get_tickets,
        "description": "Zendesk support signals: open tickets, 7d vs prior-7d volume, Sev-1 flags, 30d CSAT, per-instance breakdown.",
        "inputSchema": _ACCOUNT_ARG,
    },
    "usage_get_metrics": {
        "handler": tool_usage_get_metrics,
        "description": "Product telemetry: logins 7d vs prior, active-user %, license utilization %, key-feature adoption %, API calls, per-instance breakdown.",
        "inputSchema": _ACCOUNT_ARG,
    },
    "churn_get_score": {
        "handler": tool_churn_get_score,
        "description": "ML churn model output: churn score 0-1, model version, top risk drivers.",
        "inputSchema": _ACCOUNT_ARG,
    },
    "stripe_get_payment": {
        "handler": tool_stripe_get_payment,
        "description": "Stripe billing: past-due invoices, days past due, amount due, dunning stage (none|day_1_14|day_15_plus).",
        "inputSchema": _ACCOUNT_ARG,
    },
    "jiminny_get_calls": {
        "handler": tool_jiminny_get_calls,
        "description": "Jiminny conversational intelligence: last call date, sentiment (positive|neutral|negative), automated summary, customer talk ratio.",
        "inputSchema": _ACCOUNT_ARG,
    },
    "hubspot_push_cs_data": {
        "handler": tool_hubspot_push_cs_data,
        "description": "Bi-directional sync: push CS data (health_score, risk_status, active_playbook) back to the HubSpot company object for Sales visibility.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "account_id": {"type": "string"},
                "health_score": {"type": "number"},
                "risk_status": {"type": "string"},
                "active_playbook": {"type": "string"},
            },
            "required": ["account_id"],
        },
    },
}


# --- Minimal MCP (JSON-RPC 2.0 over stdio) ----------------------------------

def _result(request_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def handle(request: dict[str, Any]) -> dict[str, Any] | None:
    method = request.get("method")
    request_id = request.get("id")
    params = request.get("params") or {}

    if method == "initialize":
        return _result(request_id, {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "cs-stack", "version": "1.0.0"},
        })

    if method == "notifications/initialized":
        return None  # notification, no response

    if method == "tools/list":
        return _result(request_id, {
            "tools": [
                {"name": name, "description": t["description"], "inputSchema": t["inputSchema"]}
                for name, t in TOOLS.items()
            ]
        })

    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        tool = TOOLS.get(name)
        if not tool:
            return _error(request_id, -32601, f"Unknown tool: {name}")
        try:
            payload = tool["handler"](args)
        except KeyError as exc:
            return _error(request_id, -32602, str(exc))
        except Exception as exc:  # noqa: BLE001 - surface any fixture error to caller
            return _error(request_id, -32603, f"{type(exc).__name__}: {exc}")
        return _result(request_id, {
            "content": [{"type": "text", "text": json.dumps(payload, indent=2)}]
        })

    return _error(request_id, -32601, f"Unknown method: {method}")


def main() -> int:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            continue
        response = handle(request)
        if response is not None:
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

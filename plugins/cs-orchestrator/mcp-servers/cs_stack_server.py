#!/usr/bin/env python3
"""CS Orchestrator, MCP server exposing the Customer Success stack as agent tools.

This is a stdio JSON-RPC 2.0 MCP server (protocol subset: initialize, tools/list,
tools/call). Each tool returns JSON shaped to mirror the real vendor API so the
server is a drop-in swap for live integrations (HubSpot company object, Zendesk
tickets/CSAT, Stripe invoices, product usage telemetry, ML churn score).

Data source: live adapters by default. Fixture data is available only when
`CS_MCP_MODE=fixture` (explicit offline demo mode). In live mode, a missing
credential or failed live lookup fails closed rather than silently returning fixtures.

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

# Live adapters. Fixtures are an explicit offline-demo mode only.
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
MCP_MODE = os.environ.get("CS_MCP_MODE", "live").strip().lower()


def _fixture_allowed() -> bool:
    return MCP_MODE in ("fixture", "offline", "demo")


def _fixture_value(fixture_call):
    if not _fixture_allowed():
        raise RuntimeError("MCP live mode blocked fixture fallback; set CS_MCP_MODE=fixture for offline demo data")
    value = fixture_call()
    if isinstance(value, dict):
        value = dict(value)
        value["_source"] = "fixture"
    return value


def _live_or_fixture(is_live, live_call, fixture_call):
    if not _ADAPTERS or not is_live:
        return _fixture_value(fixture_call)
    try:
        return live_call()
    except Exception:
        if _fixture_allowed():
            return _fixture_value(fixture_call)
        raise


def _load() -> dict[str, Any]:
    with open(FIXTURES_PATH, encoding="utf-8") as fh:
        return json.load(fh).get("accounts", {})


def _account(account_id: str) -> dict[str, Any]:
    accounts = _load()
    if account_id not in accounts:
        raise KeyError(f"Unknown account_id '{account_id}'. Known: {', '.join(sorted(accounts))}")
    return accounts[account_id]


# --- Tool implementations ---------------------------------------------------

def _platform_account_list() -> list[dict[str, Any]] | None:
    from urllib.request import urlopen

    platform_url = os.environ.get("CS_PLATFORM_URL", "http://localhost:8787").rstrip("/")
    try:
        with urlopen(f"{platform_url}/api/accounts", timeout=3) as response:  # noqa: S310
            rows = json.loads(response.read().decode("utf-8"))
        for row in rows:
            row.setdefault("_source", "cs-platform-live-accounts")
        return rows
    except Exception:  # noqa: BLE001
        return None


def tool_list_accounts(_: dict[str, Any]) -> Any:
    platform_accounts = _platform_account_list()
    if platform_accounts is not None:
        return platform_accounts
    if _ADAPTERS and _src.HUBSPOT.live():
        return [{"account_id": aid, "_source": "hubspot-live"} for aid in _src.HUBSPOT.roster()]
    if not _fixture_allowed():
        raise RuntimeError("MCP live mode requires a live HubSpot roster; set CS_MCP_MODE=fixture for offline demo data")
    out = []
    for aid, a in _load().items():
        hs = a.get("hubspot", {})
        out.append({
            "account_id": aid,
            "name": hs.get("name"),
            "segment": hs.get("segment"),
            "arr_usd": hs.get("arr_usd"),
            "renewal_date": hs.get("renewal_date"),
            "_source": "fixture",
        })
    return out


def tool_get_portfolio_snapshot(_: dict[str, Any]) -> Any:
    """Read the live roster and all available account signals in one approval."""
    accounts = tool_list_accounts({})
    snapshot = []
    readers = {
        "hubspot": tool_hubspot_get_account,
        "zendesk": tool_zendesk_get_tickets,
        "usage": tool_usage_get_metrics,
        "churn": tool_churn_get_score,
        "stripe": tool_stripe_get_payment,
        "jiminny": tool_jiminny_get_calls,
    }
    for item in accounts:
        aid = item["account_id"]
        row = dict(item)
        row["sources"] = {}
        for source, reader in readers.items():
            try:
                row[source] = reader({"account_id": aid})
                row["sources"][source] = row[source].get("_source", "live") if isinstance(row[source], dict) else "live"
            except Exception as exc:  # noqa: BLE001
                row[source] = {"_source": "not_connected", "error": f"{type(exc).__name__}: {exc}"}
                row["sources"][source] = "not_connected"
        snapshot.append(row)
    return {"accounts": snapshot, "_source": "live-portfolio-snapshot"}


def _platform_task_queue() -> dict[str, Any] | None:
    from urllib.request import urlopen

    platform_url = os.environ.get("CS_PLATFORM_URL", "http://localhost:8787").rstrip("/")
    try:
        with urlopen(f"{platform_url}/api/portfolio", timeout=3) as response:  # noqa: S310
            portfolio = json.loads(response.read().decode("utf-8"))
        summary = portfolio.get("summary", {})
        return {
            "reviewed": summary.get("accounts", len(portfolio.get("accounts", []))),
            "tasks": portfolio.get("tasks", []),
            "suppressed": portfolio.get("suppressed", []),
            "automations": portfolio.get("automations", []),
            "judge": summary.get("judge", portfolio.get("judge", {})),
            "_source": "cs-platform-live-queue",
        }
    except Exception:  # noqa: BLE001 - fall back to direct live source fan-out
        return None


def tool_get_data_gaps(_: dict[str, Any]) -> Any:
    """Return the platform's compact live source-coverage and data-gap report."""
    from urllib.request import urlopen

    platform_url = os.environ.get("CS_PLATFORM_URL", "http://localhost:8787").rstrip("/")
    try:
        with urlopen(f"{platform_url}/api/datagaps", timeout=3) as response:  # noqa: S310
            payload = json.loads(response.read().decode("utf-8"))
        payload["_source"] = "cs-platform-live-datagaps"
        return payload
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"CS Platform data gaps unavailable: {exc}") from exc


def tool_get_csm_brief(args: dict[str, Any]) -> Any:
    """Return a CSM's managed accounts, critical context, and next governed task."""
    from urllib.request import urlopen

    platform_url = os.environ.get("CS_PLATFORM_URL", "http://localhost:8787").rstrip("/")
    try:
        with urlopen(f"{platform_url}/api/portfolio", timeout=3) as response:  # noqa: S310
            portfolio = json.loads(response.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"CS Platform CSM brief unavailable: {exc}") from exc

    summary = portfolio.get("summary", {})
    requested_owner = str(args.get("csm_owner") or "").strip()
    configured_owner = str(summary.get("scope_owner") or os.environ.get("CS_CSM_OWNER") or "").strip()
    selected_owner = requested_owner or configured_owner
    accounts = portfolio.get("accounts", [])
    available_owners = sorted({str(row.get("csm_owner")).strip() for row in accounts
                               if row.get("csm_owner")})
    if selected_owner:
        selected_accounts = [row for row in accounts
                             if str(row.get("csm_owner") or "").casefold() == selected_owner.casefold()]
    else:
        selected_accounts = accounts

    account_names = {row.get("name") for row in selected_accounts}
    owner_tasks = [task for task in portfolio.get("tasks", []) if task.get("account") in account_names]
    return {
        "csm_owner": selected_owner or None,
        "identity_status": "resolved" if selected_owner else "not_configured",
        "available_csm_owners": available_owners,
        "managed_account_count": len(selected_accounts),
        "accounts": selected_accounts,
        "next_task": owner_tasks[0] if owner_tasks else None,
        "open_tasks": owner_tasks,
        "judge": summary.get("judge", {}),
        "_source": "cs-platform-live-csm-brief",
    }


def tool_get_task_queue(_: dict[str, Any]) -> Any:
    """Build the authoritative governed queue from the live portfolio snapshot."""
    platform_queue = _platform_task_queue()
    if platform_queue is not None:
        return platform_queue

    import orchestrate

    rows = tool_get_portfolio_snapshot({})["accounts"]
    accounts = {row["account_id"]: row for row in rows}
    previous_provider = orchestrate._ACCOUNT_PROVIDER
    orchestrate.set_account_provider(lambda: accounts)
    try:
        return orchestrate.orchestrate()
    finally:
        orchestrate.set_account_provider(previous_provider)


def tool_hubspot_get_account(args: dict[str, Any]) -> Any:
    aid = args["account_id"]
    fixture = lambda: _account(aid).get("hubspot", {})
    return _live_or_fixture(_src.HUBSPOT.live() if _ADAPTERS else False,
                            lambda: _src.HUBSPOT.account(aid), fixture)


def tool_zendesk_get_tickets(args: dict[str, Any]) -> Any:
    aid = args["account_id"]
    fixture = lambda: _account(aid).get("zendesk", {})
    return _live_or_fixture(_src.ZENDESK.live() if _ADAPTERS else False,
                            lambda: _src.ZENDESK.tickets(aid), fixture)


def tool_usage_get_metrics(args: dict[str, Any]) -> Any:
    aid = args["account_id"]
    fixture = lambda: _account(aid).get("usage", {})
    return _live_or_fixture(_src.PENDO.live() if _ADAPTERS else False,
                            lambda: _src.PENDO.metrics(aid), fixture)


def tool_churn_get_score(args: dict[str, Any]) -> Any:
    aid = args["account_id"]
    fixture = lambda: _account(aid).get("churn", {})
    return _live_or_fixture(_src.CHURN.live() if _ADAPTERS else False,
                            lambda: _src.CHURN.score(aid), fixture)


def tool_stripe_get_payment(args: dict[str, Any]) -> Any:
    aid = args["account_id"]
    fixture = lambda: _account(aid).get("stripe", {})
    return _live_or_fixture(_src.STRIPE.live() if _ADAPTERS else False,
                            lambda: _src.STRIPE.payment(aid), fixture)


def tool_jiminny_get_calls(args: dict[str, Any]) -> Any:
    """Conversational intelligence: latest call sentiment, summary, talk ratio."""
    aid = args["account_id"]
    fixture = lambda: _account(aid).get("jiminny", {})
    return _live_or_fixture(_src.JIMINNY.live() if _ADAPTERS else False,
                            lambda: _src.JIMINNY.calls(aid), fixture)


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

    if not _ADAPTERS or not _src.HUBSPOT.live():
        return _fixture_value(_sim)
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
    "get_csm_brief": {
        "handler": tool_get_csm_brief,
        "description": "Return the configured or requested CSM, all accounts they manage with critical health/lifecycle/ARR/renewal context, and their next governed task. Use for 'who is my CSM', 'my accounts', and 'what should I work on next' questions.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "csm_owner": {"type": "string", "description": "Optional CSM owner name. Omit to use configured scope or list available owners."}
            },
        },
    },
    "get_data_gaps": {
        "handler": tool_get_data_gaps,
        "description": "Return the compact live CS data-gap report, including source coverage, unavailable account mappings, missing HubSpot fields, and missing contact roles. Prefer this for data-gap and source-coverage questions.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    "get_task_queue": {
        "handler": tool_get_task_queue,
        "description": "Return the authoritative prioritised CS task queue built from the live portfolio, including evidence, suppression, automations, and the WoW judge verdict. Prefer this for next-action and top-actions questions.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    "get_portfolio_snapshot": {
        "handler": tool_get_portfolio_snapshot,
        "description": "Read the complete live CS portfolio and all available source signals in one read-only operation. Prefer this for portfolio questions.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    "list_accounts": {
        "handler": tool_list_accounts,
        "description": "List all live CS accounts with owner, health, lifecycle, segment, ARR, renewal, source connectivity, and open-task count. Use as the fallback for CSM account-book questions.",
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
}

_READ_ONLY_ANNOTATIONS = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
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
                {"name": name, "description": t["description"],
                 "inputSchema": t["inputSchema"], "annotations": _READ_ONLY_ANNOTATIONS}
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

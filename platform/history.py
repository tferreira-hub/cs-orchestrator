"""Health-history snapshots and trend deltas (Customer 360 §4).

The platform's scores are computed live and point-in-time. To detect trajectories
("health declined 18 points over 45 days", "usage down 24% in 30 days") we persist a
daily snapshot per account and expose trend helpers. Append-only JSONL, one line per
(account_id, date) so re-running on the same day overwrites that day's value in memory
but never rewrites history destructively.

Storage is a local JSONL file by default; set CS_HISTORY_FILE to relocate it (e.g. an
EFS mount or a synced volume) so history survives task restarts. This is deliberately
simple, no time-series DB is warranted at this scale (Customer 360 §18: do not introduce
services just because they are available).
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta
from pathlib import Path
from threading import Lock

HISTORY_FILE = Path(os.environ.get(
    "CS_HISTORY_FILE",
    str(Path(__file__).resolve().parents[1] / ".cs-health-history.jsonl")))
_LOCK = Lock()


def _today() -> str:
    env = os.environ.get("CS_TODAY")
    try:
        return (datetime.fromisoformat(env).date() if env else date.today()).isoformat()
    except ValueError:
        return date.today().isoformat()


def record_snapshot(account_id: str, snapshot: dict, on_day: str | None = None) -> None:
    """Append one health snapshot for an account for a day. Idempotent per day: if a
    snapshot for (account_id, day) already exists, this is skipped so a busy platform
    that loads the portfolio many times a day stores one point per day."""
    if not account_id:
        return
    day = on_day or _today()
    with _LOCK:
        if _has_snapshot(account_id, day):
            return
        row = {"account_id": account_id, "date": day, **snapshot}
        HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        with HISTORY_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, default=str) + "\n")


def record_portfolio(accounts: dict, health_fn, on_day: str | None = None) -> int:
    """Snapshot the health of every account once for the day. `health_fn(account)` must
    return the health dict {score, band, ...}. Returns the number of new snapshots."""
    day = on_day or _today()
    written = 0
    for aid, acct in (accounts or {}).items():
        try:
            h = health_fn(acct)
            if not h.get("computable"):
                continue
            usage = acct.get("usage", {}) if isinstance(acct, dict) else {}
            snap = {
                "health": h.get("score"),
                "band": h.get("band"),
                "churn": (acct.get("churn", {}) or {}).get("ml_churn_score"),
                "days_since_visit": usage.get("days_since_last_visit"),
                "logins_7d": usage.get("logins_last_7d"),
            }
            before = _has_snapshot(aid, day)
            record_snapshot(aid, snap, on_day=day)
            if not before:
                written += 1
        except Exception:  # noqa: BLE001
            continue
    return written


def _all_rows() -> list[dict]:
    if not HISTORY_FILE.exists():
        return []
    rows = []
    for line in HISTORY_FILE.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                rows.append(json.loads(line))
            except Exception:  # noqa: BLE001
                pass
    return rows


def _has_snapshot(account_id: str, day: str) -> bool:
    for r in _all_rows():
        if r.get("account_id") == account_id and r.get("date") == day:
            return True
    return False


def history_for(account_id: str) -> list[dict]:
    rows = [r for r in _all_rows() if r.get("account_id") == account_id]
    rows.sort(key=lambda r: r.get("date", ""))
    return rows


def health_trend(account_id: str, window_days: int = 45) -> dict | None:
    """Return the health delta over the window: {current, past, delta, days, direction}.
    None when there is not enough history to be meaningful."""
    rows = [r for r in history_for(account_id) if r.get("health") is not None]
    if len(rows) < 2:
        return None
    current = rows[-1]
    try:
        cur_day = datetime.fromisoformat(current["date"]).date()
    except (ValueError, KeyError):
        return None
    cutoff = cur_day - timedelta(days=window_days)
    # Earliest snapshot on/after the cutoff (so the window is honoured, not the whole history).
    past = None
    for r in rows[:-1]:
        try:
            d = datetime.fromisoformat(r["date"]).date()
        except (ValueError, KeyError):
            continue
        if d >= cutoff:
            past = r
            break
    if past is None:
        past = rows[0]  # fall back to the oldest we have
    try:
        days = (cur_day - datetime.fromisoformat(past["date"]).date()).days
    except (ValueError, KeyError):
        return None
    if days <= 0:
        return None
    delta = round(current["health"] - past["health"])
    return {
        "current": current["health"], "past": past["health"], "delta": delta,
        "days": days, "direction": "up" if delta > 0 else "down" if delta < 0 else "flat",
    }

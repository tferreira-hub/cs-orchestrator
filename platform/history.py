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

# In-memory cache of the parsed history, validated against the file's mtime+size so an
# external writer (another task on the shared EFS mount, a restart) is picked up, but
# repeated reads in one process do not re-read+re-parse the whole file. Without this,
# record_portfolio() was O(accounts x filesize): 4,300 accounts each triggering a full
# file scan via _has_snapshot -> _all_rows, which on the EFS-backed prod volume took so
# long the boot warm never finished and the dashboard stayed stuck on "Checking sources".
_CACHE_ROWS: list[dict] | None = None
_CACHE_SIG: tuple[float, int] | None = None          # (mtime, size) of the file when cached
_CACHE_INDEX: set[tuple[str, str]] = set()           # {(account_id, date)} for O(1) existence


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
        # Keep the in-memory cache coherent with our own append so the rest of a
        # record_portfolio() loop stays O(1) and does not re-read the growing file. We
        # refresh the signature to the post-write (mtime,size) so the next read trusts
        # the cache rather than reloading from disk.
        if _CACHE_ROWS is not None:
            _CACHE_ROWS.append(row)
            _CACHE_INDEX.add((account_id, day))
            globals()["_CACHE_SIG"] = _file_sig()


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
            ent = acct.get("entitlements", {}) if isinstance(acct, dict) else {}
            hs = acct.get("hubspot", {}) if isinstance(acct, dict) else {}
            snap = {
                "health": h.get("score"),
                "band": h.get("band"),
                "churn": (acct.get("churn", {}) or {}).get("ml_churn_score"),
                "days_since_visit": usage.get("days_since_last_visit"),
                "logins_7d": usage.get("logins_last_7d"),
                # Active seats from the entitlement system (None when Entitlements is not
                # connected or has no record — never fabricated). Powers the seat side of
                # the sudden-contraction rule.
                "seats": (ent.get("active_seats") if isinstance(ent, dict) else None),
                # Subscription type over time so an in-quarter Month-to-Month -> fixed-term
                # MOVE is detectable (a true move count, not just the current split). None
                # when not populated; never fabricated.
                "subscription_type": (hs.get("subscription_type") if isinstance(hs, dict) else None),
            }
            before = _has_snapshot(aid, day)
            record_snapshot(aid, snap, on_day=day)
            if not before:
                written += 1
        except Exception:  # noqa: BLE001
            continue
    return written


def _file_sig() -> tuple[float, int] | None:
    try:
        st = HISTORY_FILE.stat()
        return (st.st_mtime, st.st_size)
    except FileNotFoundError:
        return None


def _refresh_cache() -> None:
    """(Re)load the history into memory if the file changed since we last read it. Builds
    both the row list and the (account_id, date) existence index. Must hold nothing; the
    public callers that mutate state hold _LOCK."""
    global _CACHE_ROWS, _CACHE_SIG, _CACHE_INDEX
    sig = _file_sig()
    if _CACHE_ROWS is not None and sig == _CACHE_SIG:
        return
    rows: list[dict] = []
    index: set[tuple[str, str]] = set()
    if sig is not None:
        for line in HISTORY_FILE.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                r = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            rows.append(r)
            aid, dt = r.get("account_id"), r.get("date")
            if aid is not None and dt is not None:
                index.add((aid, dt))
    _CACHE_ROWS, _CACHE_SIG, _CACHE_INDEX = rows, sig, index


def _all_rows() -> list[dict]:
    _refresh_cache()
    return list(_CACHE_ROWS or [])


def _has_snapshot(account_id: str, day: str) -> bool:
    _refresh_cache()
    return (account_id, day) in _CACHE_INDEX


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


def _pct_change_over_window(rows: list[dict], field: str, window_days: int):
    """(from_value, to_value, pct_change, days) for a numeric snapshot field over the
    window, or None when there aren't two comparable points. pct_change is negative for a
    drop. Mirrors health_trend()'s windowing: compares the latest point to the earliest
    point on/after (latest - window)."""
    pts = [r for r in rows if isinstance(r.get(field), (int, float))]
    if len(pts) < 2:
        return None
    current = pts[-1]
    try:
        cur_day = datetime.fromisoformat(current["date"]).date()
    except (ValueError, KeyError):
        return None
    cutoff = cur_day - timedelta(days=window_days)
    past = None
    for r in pts[:-1]:
        try:
            d = datetime.fromisoformat(r["date"]).date()
        except (ValueError, KeyError):
            continue
        if d >= cutoff:
            past = r
            break
    if past is None:
        past = pts[0]
    try:
        days = (cur_day - datetime.fromisoformat(past["date"]).date()).days
    except (ValueError, KeyError):
        return None
    if days <= 0:
        return None
    from_v = past[field]
    to_v = current[field]
    if not from_v:  # avoid div-by-zero; a rise from 0 isn't a contraction
        return None
    pct = round(100 * (to_v - from_v) / from_v)
    return (from_v, to_v, pct, days)


def usage_contraction(account_id: str, window_days: int = 14, drop_pct: int = 20) -> dict | None:
    """Detect a SUDDEN contraction in active users (logins_7d) or seats over a rolling
    window (default 14 days), independent of renewal date (V5 UC1/UC2).

    Returns a dict describing the drop, with `contracted` True when logins OR seats fell
    by more than `drop_pct`. Returns None when there isn't enough comparable history for
    either signal (data-gap, never fabricated). The seat side only fires when the
    Entitlements system has supplied seat counts in the snapshots; the login side works
    from live Pendo logins today."""
    rows = history_for(account_id)
    logins = _pct_change_over_window(rows, "logins_7d", window_days)
    seats = _pct_change_over_window(rows, "seats", window_days)
    if logins is None and seats is None:
        return None

    logins_drop = bool(logins and logins[2] <= -drop_pct)
    seats_drop = bool(seats and seats[2] <= -drop_pct)
    driver = ("both" if logins_drop and seats_drop
              else "logins" if logins_drop
              else "seats" if seats_drop
              else None)
    return {
        "contracted": bool(driver),
        "driver": driver,
        "window_days": window_days,
        "threshold_pct": drop_pct,
        "logins_from": logins[0] if logins else None,
        "logins_to": logins[1] if logins else None,
        "logins_pct_change": logins[2] if logins else None,
        "seats_from": seats[0] if seats else None,
        "seats_to": seats[1] if seats else None,
        "seats_pct_change": seats[2] if seats else None,
        "days": (logins[3] if logins else (seats[3] if seats else None)),
    }


def portfolio_trajectory(max_points: int = 30) -> dict:
    """Portfolio-wide health trajectory: average computed-health per day across all
    account snapshots, oldest -> newest. Powers the dashboard 'Portfolio Development'
    widget from the real .cs-health-history snapshots (no fabrication). Returns
    {"points": [{"date","avg_health","accounts"}...]} with at most max_points days."""
    rows = [r for r in _all_rows() if r.get("health") is not None]
    by_day: dict[str, list[int]] = {}
    for r in rows:
        d = r.get("date")
        if not d:
            continue
        by_day.setdefault(d, []).append(r["health"])
    points = []
    for day in sorted(by_day):
        vals = by_day[day]
        points.append({
            "date": day,
            "avg_health": round(sum(vals) / len(vals)),
            "accounts": len(vals),
        })
    if max_points and len(points) > max_points:
        points = points[-max_points:]
    return {"points": points}


def timeline_for(account_id: str, extra_events: list | None = None) -> list[dict]:
    """Build a chronological customer timeline (Customer 360 §11) for one account.

    Derives events from the health-snapshot history (notable health moves) and merges any
    caller-supplied events (task status changes, risks, agent actions). Returned newest
    first so the UI can show a running story of what happened to the customer and when."""
    events: list[dict] = []
    rows = [r for r in history_for(account_id) if r.get("health") is not None]
    prev = None
    for r in rows:
        if prev is not None:
            delta = r["health"] - prev["health"]
            if abs(delta) >= 5:  # only notable moves, not noise
                events.append({
                    "date": r.get("date"),
                    "type": "health_change",
                    "title": f"Health {'up' if delta > 0 else 'down'} {abs(round(delta))} points ({prev['health']} to {r['health']})",
                    "severity": "positive" if delta > 0 else "watch" if delta > -12 else "risk",
                })
        prev = r
    if rows:
        first = rows[0]
        events.append({"date": first.get("date"), "type": "tracking_started",
                       "title": f"Health tracking began at {first.get('health')}",
                       "severity": "info"})
    for e in (extra_events or []):
        if e:
            events.append(e)
    events.sort(key=lambda e: e.get("date", ""), reverse=True)
    return events

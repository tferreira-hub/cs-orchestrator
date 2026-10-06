#!/usr/bin/env python3
"""CS Platform, HTTP API + static UI host.

Dependency-free (Python stdlib only) so it runs anywhere with no pip install,
ideal for a hackathon demo. Serves the single-pane-of-glass UI and a small JSON API
backed by the same WoW orchestration engine used by the CLI and the agent harness.

Run:  python3 platform/server.py           # http://localhost:8787
Endpoints:
  GET /api/portfolio            -> summary + accounts (health) + task queue + suppressed
  GET /api/accounts             -> accounts with health
  GET /api/accounts/{id}        -> full account detail (signals, health, tasks, suppressed)
  GET /api/tasks                -> prioritised task queue
  GET /api/suppressed           -> suppressed multi-instance signals
  GET /                         -> the CSM dashboard UI
"""

from __future__ import annotations

import json
import hashlib
import os
import sys
import uuid
import urllib.parse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def _load_dotenv() -> None:
    """Load KEY=VALUE lines from a repo-root .env into os.environ (no dependency on
    python-dotenv). Existing environment variables take precedence, so an explicit
    export always wins over the file. Values may be quoted with single or double
    quotes; escaped inner quotes (\\' / \\") are un-escaped. Blank lines and
    #comments are ignored."""
    # repo root is two levels up from platform/server.py
    for candidate in (Path(__file__).resolve().parent / ".env",
                      Path(__file__).resolve().parents[1] / ".env"):
        if not candidate.exists():
            continue
        for raw in candidate.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip()
            # Strip matched outer quotes and un-escape inner escaped quotes.
            if len(val) >= 2 and val[0] == val[-1] and val[0] in ('"', "'"):
                quote = val[0]
                val = val[1:-1].replace(f"\\{quote}", quote)
            if key and key not in os.environ:
                os.environ[key] = val
        break


_load_dotenv()

sys.path.insert(0, str(Path(__file__).resolve().parent))
import engine  # noqa: E402
import auth  # noqa: E402
import rbac  # noqa: E402
import tableau  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins" / "cs-orchestrator"))
from adapters import sources as _src  # noqa: E402
import inbound as _inbound  # noqa: E402

# Transient OIDC flow state (state -> code_verifier), in-process. Fine for a single
# server; a multi-instance deployment would use a shared store.
_OIDC_FLOWS: dict[str, str] = {}

# Static HTML pages (auth screens + SSO landing) live in pages.py — pure markup,
# no server logic. Imported here so the request handler stays focused.
from pages import (  # noqa: E402
    _DEV_LOGIN_HTML,
    _SIGNED_OUT_HTML,
    _NOT_AUTHORISED_HTML,
    _SSO_LOGIN_HTML,
)

UI_PATH = Path(__file__).resolve().parent / "ui" / "index.html"
PORT = int(os.environ.get("CS_PORT", "8787"))
FEEDBACK_LOG = Path(__file__).resolve().parents[1] / ".cs-agent-feedback.jsonl"
PLAYBOOK_PROPOSALS = Path(__file__).resolve().parents[1] / ".cs-playbook-proposals.jsonl"
PLAYBOOK_RULES = Path(__file__).resolve().parents[1] / "plugins" / "cs-orchestrator" / "skills" / "cs-playbook" / "SKILL.md"
TASK_EVENTS = Path(os.environ.get("CS_TASK_EVENTS_FILE", str(Path(__file__).resolve().parents[1] / ".cs-task-events.jsonl")))
# Immutable, hash-chained audit trail (enterprise compliance): every AI-suggested action
# and CSM approval is appended with the hash of the previous entry, so any tampering with
# an earlier record breaks the chain and is detectable.
AUDIT_LOG = Path(os.environ.get("CS_AUDIT_FILE", str(Path(__file__).resolve().parents[1] / ".cs-audit-trail.jsonl")))
# Success Plans / Goals (Customer 360 §8): explicit customer goals with target, baseline,
# deadline and milestones. Append-only JSONL; the latest record per (account, goal) wins.
SUCCESS_PLANS = Path(os.environ.get("CS_SUCCESS_PLANS_FILE", str(Path(__file__).resolve().parents[1] / ".cs-success-plans.jsonl")))
import hashlib as _hashlib


def _record_success_plan(body: dict, principal: dict | None) -> dict:
    account_id = str(body.get("account_id") or "").strip()
    goal = str(body.get("goal") or "").strip()
    if not account_id or not goal:
        raise ValueError("account_id and goal are required")
    plan = {
        "plan_id": body.get("plan_id") or uuid.uuid4().hex[:12],
        "account_id": account_id,
        "goal": goal,
        "metric": str(body.get("metric") or "").strip() or None,
        "baseline": body.get("baseline"),
        "target": body.get("target"),
        "deadline": str(body.get("deadline") or "").strip() or None,
        "status": body.get("status") or "on_track",
        "milestones": body.get("milestones") if isinstance(body.get("milestones"), list) else [],
        "created_by": (principal or {}).get("email") or (principal or {}).get("name"),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    SUCCESS_PLANS.parent.mkdir(parents=True, exist_ok=True)
    with SUCCESS_PLANS.open("a", encoding="utf-8") as f:
        f.write(json.dumps(plan, default=str) + "\n")
    try:
        record_audit("success_plan_created", principal, {"account_id": account_id, "goal": goal})
    except Exception:  # noqa: BLE001
        pass
    return plan


def _success_plans_for(account_id: str) -> list[dict]:
    if not SUCCESS_PLANS.exists():
        return []
    latest: dict[str, dict] = {}
    for line in SUCCESS_PLANS.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        if row.get("account_id") == account_id:
            latest[row.get("plan_id")] = row  # later lines overwrite earlier (edits)
    return sorted(latest.values(), key=lambda r: r.get("created_at", ""))



def _audit_last_hash() -> str:
    if not AUDIT_LOG.exists():
        return "genesis"
    last = ""
    for line in AUDIT_LOG.read_text(encoding="utf-8").splitlines():
        if line.strip():
            last = line
    if not last:
        return "genesis"
    try:
        return json.loads(last).get("entry_hash", "genesis")
    except Exception:  # noqa: BLE001
        return "genesis"


def record_audit(event_type: str, principal: dict | None, detail: dict) -> dict:
    """Append an immutable, hash-chained audit entry. Records WHO (principal), WHAT
    (event_type + detail: rule/prompt, data sources read), and WHEN. Never stores customer
    prose; detail should be metadata (account ids, rule ids, source names)."""
    prev = _audit_last_hash()
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": event_type,
        "actor": {"email": (principal or {}).get("email"),
                  "name": (principal or {}).get("name"),
                  "role": (principal or {}).get("role")} if principal else {"role": "system"},
        "detail": detail,
        "prev_hash": prev,
    }
    entry["entry_hash"] = _hashlib.sha256(
        (prev + json.dumps(entry, sort_keys=True, default=str)).encode("utf-8")).hexdigest()
    AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
    with AUDIT_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, default=str) + "\n")
    return entry


def _audit_read(limit: int = 200) -> dict:
    """Return recent audit entries plus a chain-integrity verdict."""
    entries = []
    if AUDIT_LOG.exists():
        for line in AUDIT_LOG.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    entries.append(json.loads(line))
                except Exception:  # noqa: BLE001
                    pass
    # Verify the hash chain end to end.
    intact, prev = True, "genesis"
    for e in entries:
        recomputed = _hashlib.sha256(
            (prev + json.dumps({k: e[k] for k in ("ts", "event", "actor", "detail", "prev_hash") if k in e},
                               sort_keys=True, default=str)).encode("utf-8")).hexdigest()
        if e.get("prev_hash") != prev or e.get("entry_hash") != recomputed:
            intact = False
        prev = e.get("entry_hash", prev)
    return {"entries": entries[-limit:], "count": len(entries), "chain_intact": intact}


def _record_task_event(body: dict) -> dict:
    task_id = str(body.get("task_id") or "").strip()
    status = body.get("status")
    if not task_id or status not in {"open", "in_progress", "completed"}:
        raise ValueError("task_id and status (open, in_progress, completed) are required")
    event = {
        "task_id": task_id,
        "status": status,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    if status == "completed":
        event["completed_at"] = str(body.get("completed_at") or event["updated_at"])
    TASK_EVENTS.parent.mkdir(parents=True, exist_ok=True)
    with TASK_EVENTS.open("a", encoding="utf-8") as event_file:
        event_file.write(json.dumps(event) + "\n")
    return event


def _record_agent_feedback(body: dict) -> dict:
    """Persist only feedback metadata, never customer prose or answer content."""
    question = str(body.get("question") or "")
    display_reason = str(body.get("reason") or "other").strip()
    raw_reason = display_reason.lower().replace(" ", "_")
    record = {
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "rating": body.get("rating"),
        "reason": raw_reason if raw_reason in {
            "wrong_priority", "missing_evidence", "irrelevant_action", "source_gap", "other"
        } else "other",
        "reason_label": display_reason,
        "question_hash": hashlib.sha256(question.encode()).hexdigest() if question else None,
        "action_count": int(body.get("action_count") or 0),
        "judge_verdict": body.get("judge_verdict"),
    }
    with FEEDBACK_LOG.open("a", encoding="utf-8") as feedback_file:
        feedback_file.write(json.dumps(record) + "\n")
    return {"recorded": True, "recorded_at": record["recorded_at"]}


def _feedback_summary() -> dict:
    counts = {"helpful": 0, "needs_correction": 0}
    reasons = {}
    if FEEDBACK_LOG.exists():
        for line in FEEDBACK_LOG.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            rating = row.get("rating")
            if rating in counts:
                counts[rating] += 1
            reason = row.get("reason")
            if rating == "needs_correction" and reason:
                reasons[reason] = reasons.get(reason, 0) + 1
    recommendations = {
        "wrong_priority": "Review trigger-to-priority mappings in orchestrate.py and add a boundary test.",
        "missing_evidence": "Require the missing source field in the playbook judge before allowing PASS.",
        "irrelevant_action": "Review the WoW rule and specialist prompt for this action category.",
        "source_gap": "Add or repair the source mapping, then add a live adapter contract test.",
        "other": "Review the feedback sample during the next CS playbook calibration session.",
    }
    candidates = [{"category": reason, "count": count, "recommendation": recommendations.get(reason, recommendations["other"])}
                  for reason, count in sorted(reasons.items(), key=lambda item: -item[1])]
    return {"ratings": counts, "correction_reasons": reasons, "improvement_candidates": candidates,
            "privacy": "metadata-only"}


def _playbook_summary() -> dict:
    text = PLAYBOOK_RULES.read_text(encoding="utf-8") if PLAYBOOK_RULES.exists() else ""
    headings = [line.lstrip("# ").strip() for line in text.splitlines()
                if line.startswith("## ") or line.startswith("### ")]
    proposals_by_id = {}
    if PLAYBOOK_PROPOSALS.exists():
        for line in PLAYBOOK_PROPOSALS.read_text(encoding="utf-8").splitlines():
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            proposal_id = record.get("proposal_id")
            if not proposal_id:
                continue
            if record.get("event") == "review":
                current = proposals_by_id.get(proposal_id)
                if current:
                    current.update({key: value for key, value in record.items() if key != "event"})
                    current.setdefault("review_history", []).append(record)
            else:
                proposals_by_id[proposal_id] = record
                record.setdefault("review_history", [])
    proposals = [proposal for proposal in proposals_by_id.values()
                 if proposal.get("status") != "archived"]
    return {"status": "signed-and-versioned", "headings": headings,
            "proposals": proposals[-20:],
            "change_policy": "CS feedback creates proposals; approved changes require review, tests, signed skill versioning, and a release.",
            "review_decisions": ["request_changes", "approve_for_implementation", "reject", "archive"]}


def _record_playbook_proposal(body: dict) -> dict:
    proposal = {
        "proposal_id": hashlib.sha256((datetime.now(timezone.utc).isoformat() + str(body)).encode()).hexdigest()[:12],
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": "pending_review",
        "category": body.get("category", "other"),
        "rule_section": str(body.get("rule_section") or "Other")[:160],
        "evidence_source": str(body.get("evidence_source") or "")[:500],
        "quality_risk": str(body.get("quality_risk") or "")[:500],
        "review_stage": "CS Leadership",
        "review_checklist": {
            "evidence_verified": False,
            "policy_conflict_checked": False,
            "tests_added": False,
            "rollback_defined": False,
        },
        "title": str(body.get("title") or "")[:160],
        "rationale": str(body.get("rationale") or "")[:1000],
        "requested_by": str(body.get("requested_by") or "CS team")[:100],
        "current_behavior": str(body.get("current_behavior") or "")[:1000],
        "proposed_behavior": str(body.get("proposed_behavior") or "")[:1000],
        "consumer_context": str(body.get("consumer_context") or "")[:1000],
        "impact": str(body.get("impact") or "")[:1000],
        "affected_accounts": str(body.get("affected_accounts") or "")[:500],
        "test_cases": str(body.get("test_cases") or "")[:1000],
    }
    if not proposal["title"] or not proposal["rationale"]:
        raise ValueError("title and rationale are required")
    with PLAYBOOK_PROPOSALS.open("a", encoding="utf-8") as proposal_file:
        proposal_file.write(json.dumps(proposal) + "\n")
    return proposal


def _record_playbook_review(proposal_id: str, body: dict) -> dict:
    decision = str(body.get("decision") or "").strip().lower()
    allowed = {"request_changes", "approve_for_implementation", "reject", "archive"}
    if decision not in allowed:
        raise ValueError("decision must be request_changes, approve_for_implementation, reject, or archive")
    reviewer = str(body.get("reviewed_by") or "").strip()
    review_note = str(body.get("review_note") or "").strip()
    if not reviewer:
        raise ValueError("reviewed_by is required")
    if not review_note:
        raise ValueError("review_note is required")
    checklist = body.get("review_checklist") or {}
    required_checks = {"evidence_verified", "policy_conflict_checked", "tests_added", "rollback_defined"}
    if decision == "approve_for_implementation" and not all(checklist.get(key) is True for key in required_checks):
            raise ValueError("approval requires all quality gates: evidence, policy, tests, and rollback checks")
    summary = _playbook_summary()
    proposal = next((item for item in summary["proposals"] if item.get("proposal_id") == proposal_id), None)
    if not proposal:
        raise ValueError(f"unknown proposal {proposal_id}")
    status = {
        "request_changes": "changes_requested",
        "approve_for_implementation": "approved_for_implementation",
        "reject": "rejected",
        "archive": "archived",
    }[decision]
    review = {
        "event": "review",
        "proposal_id": proposal_id,
        "decision": decision,
        "status": status,
        "review_stage": str(body.get("review_stage") or "CS Leadership")[:80],
        "reviewed_by": reviewer[:120],
        "review_note": review_note[:1000],
        "review_checklist": {key: bool(checklist.get(key)) for key in required_checks},
        "reviewed_at": datetime.now(timezone.utc).isoformat(),
    }
    with PLAYBOOK_PROPOSALS.open("a", encoding="utf-8") as proposal_file:
        proposal_file.write(json.dumps(review) + "\n")
    return {**proposal, **{key: value for key, value in review.items() if key != "event"}}


class Handler(BaseHTTPRequestHandler):
    def _cors_origin(self) -> str | None:
        """The single allowed CORS origin, or None.

        The UI is served same-origin from this server, so cross-origin access is not
        required for the app to work. We therefore never emit a wildcard
        `Access-Control-Allow-Origin: *` on authenticated API JSON. An operator who
        genuinely needs a cross-origin caller sets CS_CORS_ORIGIN (or falls back to the
        app's own public origin CS_PUBLIC_URL) to a SINGLE explicit origin; anything
        else means no CORS header at all."""
        origin = (os.environ.get("CS_CORS_ORIGIN") or os.environ.get("CS_PUBLIC_URL") or "").strip()
        return origin.rstrip("/") or None

    def _send(self, code: int, body: bytes, content_type: str, extra_headers: dict | None = None) -> None:
        try:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            # Scoped CORS: emit a single explicit allowed origin when configured, never a
            # wildcard (which on an authenticated JSON API is a cross-site exposure). The
            # app is same-origin, so by default no ACAO header is sent at all.
            cors_origin = self._cors_origin()
            if cors_origin:
                self.send_header("Access-Control-Allow-Origin", cors_origin)
                self.send_header("Vary", "Origin")
            self.send_header("Content-Length", str(len(body)))
            # The SPA shell and API responses are per-session and must never be cached
            # by the browser or the Cloudflare edge; a cached shell can re-boot with a
            # stale auth state and look like a reload. Static-free app, so no-store is safe.
            if "text/html" in content_type or "application/json" in content_type:
                self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
                self.send_header("Pragma", "no-cache")
            for k, v in (extra_headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if not getattr(self, "_head_only", False):
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            # The browser navigated away or cancelled the request before delivery.
            return

    def _json(self, code: int, payload, extra_headers: dict | None = None) -> None:
        self._send(code, json.dumps(payload).encode("utf-8"), "application/json", extra_headers)

    # --- Authentication helpers ------------------------------------------- #
    def _principal(self) -> dict | None:
        """Decode the signed session cookie into a principal, or None."""
        cookies = auth.parse_cookies(self.headers.get("Cookie"))
        token = cookies.get(auth.session_cookie_name())
        return auth.read_session(token)

    def _redirect(self, location: str, extra_headers: dict | None = None) -> None:
        headers = {"Location": location}
        if extra_headers:
            headers.update(extra_headers)
        self._send(302, b"", "text/plain", headers)

    def _redirect_uri(self) -> str:
        """The OIDC callback URL for this server (honour a configured public URL)."""
        base = os.environ.get("CS_PUBLIC_URL")
        if base:
            return base.rstrip("/") + "/auth/callback"
        host = self.headers.get("Host", "localhost:8787")
        scheme = "https" if os.environ.get("CS_SECURE_COOKIE") else "http"
        return f"{scheme}://{host}/auth/callback"

    def _finish_login(self, principal_core: dict, extra_set_cookie: str | None = None) -> None:
        """Resolve the HubSpot owner for the authenticated email, mint the session
        cookie, and redirect to the dashboard. Enforces the CS Platform entitlement:
        a user who authenticated but is not entitled (no CS admin/user group) is
        DENIED — no session is minted — rather than admitted as a scoped CSM."""
        email = principal_core.get("email", "")
        # Hard entitlement gate (defense-in-depth behind the UI launcher).
        allowed = principal_core.get("has_access")
        if allowed is None:  # dev-login path builds a minimal core
            allowed = rbac.has_cs_access(email, principal_core.get("groups", ""))
        if not allowed:
            # Diagnostic: log exactly what the IdP delivered so we can tell a missing
            # group assignment from a name-vs-id mismatch. No secrets, groups only.
            import sys as _sys
            print(f"[cs-auth] access denied email={email!r} "
                  f"groups={principal_core.get('groups','')!r} "
                  f"admin_groups={rbac._admin_groups()!r} user_groups={rbac._user_groups()!r} "
                  f"admin_emails_set={bool(rbac._admin_emails())}", file=_sys.stderr, flush=True)
            self._send(403, _NOT_AUTHORISED_HTML.encode("utf-8"), "text/html; charset=utf-8")
            return
        owner_id = None
        try:
            owner_id = _src.HUBSPOT.owner_id_for_email(email) if _src.HUBSPOT.live() else None
        except Exception:  # noqa: BLE001
            owner_id = None
        token = auth.make_session(
            email=email, name=principal_core.get("name") or email,
            role=principal_core.get("role", rbac.CSM), owner_id=owner_id,
            groups=principal_core.get("groups", ""))
        # Emit the session cookie and (on the OIDC path) clear the transient flow
        # cookie — two separate Set-Cookie headers.
        cookies = [auth.cookie_header(token)]
        if extra_set_cookie:
            cookies.append(extra_set_cookie)
        self._redirect_cookies("/", cookies)

    def _redirect_cookies(self, location: str, set_cookies: list[str]) -> None:
        """302 redirect emitting one or more Set-Cookie headers."""
        try:
            self.send_response(302)
            self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            for c in set_cookies:
                self.send_header("Set-Cookie", c)
            self.end_headers()
        except (BrokenPipeError, ConnectionResetError):
            return

    def _handle_login(self, query: dict | None = None) -> None:
        query = query or {}
        # After sign-out we land here with ?logged_out=1. Render an explicit signed-out
        # screen instead of bouncing straight back to Cognito (which would silently
        # re-authenticate the user). The user clicks "Sign in again" to start a new flow.
        if (query.get("logged_out") or [None])[0] == "1":
            self._send(200, _SIGNED_OUT_HTML.encode("utf-8"), "text/html; charset=utf-8")
            return
        # Dev-login: no Cognito needed. Renders a tiny form that posts an email.
        if not auth.cognito_configured() and auth.dev_login_enabled():
            self._send(200, _DEV_LOGIN_HTML.encode("utf-8"), "text/html; charset=utf-8")
            return
        # Production SSO. We serve a branded CS Platform landing page FIRST and only
        # start the Cognito/Okta OIDC round-trip when the user clicks "Sign in with SSO"
        # (which comes back as ?sso=1). This gives users a proper CS Platform page rather
        # than an abrupt redirect to the generic Cognito hosted UI.
        if (query.get("sso") or [None])[0] != "1":
            err = (query.get("error") or [None])[0]
            err_html = ""
            if err:
                # Show a friendly, escaped error banner (e.g. invalid_state) without
                # leaking internals.
                import html as _html
                err_html = f'<div class=err>Sign-in could not be completed ({_html.escape(err)}). Please try again.</div>'
            page = _SSO_LOGIN_HTML.replace("__ERROR__", err_html)
            self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")
            return
        # OIDC: start Authorization Code + PKCE against Cognito. The state+verifier
        # go in a signed, short-lived cookie (NOT server memory) so any task behind
        # the ALB can complete the callback — in-memory state caused a redirect loop
        # under >1 replica.
        import secrets as _secrets
        verifier, challenge = auth.pkce_pair()
        state = _secrets.token_urlsafe(24)
        flow = auth.make_flow_token(state, verifier)
        self._redirect(auth.authorize_url(self._redirect_uri(), state, challenge),
                       {"Set-Cookie": auth.flow_cookie_header(flow)})

    def _handle_callback(self, query: dict) -> None:
        code = (query.get("code") or [None])[0]
        state = (query.get("state") or [None])[0]
        cookies = auth.parse_cookies(self.headers.get("Cookie"))
        flow = auth.read_flow_token(cookies.get(auth.flow_cookie_name()))
        # State must match the value bound into the signed flow cookie (CSRF guard).
        if not code or not flow or not state or flow.get("state") != state:
            self._redirect("/login?error=invalid_state"); return
        verifier = flow.get("verifier")
        try:
            tokens = auth.exchange_code(code, self._redirect_uri(), verifier)
            info = auth.fetch_userinfo(tokens["access_token"])
            self._finish_login(auth.principal_from_userinfo(info),
                               extra_set_cookie=auth.clear_flow_cookie_header())
        except Exception as exc:  # noqa: BLE001
            self._redirect(f"/login?error={urllib.parse.quote(type(exc).__name__)}")

    def _handle_dev_login(self, body: dict) -> None:
        """Dev-login POST: trust the submitted email (LOCAL ONLY — gated by
        AUTH_DEV_LOGIN and never active once Cognito is configured)."""
        email = str(body.get("email") or "").strip()
        if not email:
            self._json(400, {"error": "email required"}); return
        role = rbac.resolve_role(email, None)
        self._finish_login({"email": email, "name": email, "role": role, "groups": ""})

    def _handle_logout(self) -> None:
        # Clear the local session cookie and land on the CS signed-out screen. We do NOT
        # do a Cognito federated logout: with SAML SSO (Identity Center → Okta) that flow
        # bounces the user to the AWS Identity Center portal instead of back to the CS app.
        # Clearing the local cookie ends the CS session; /login?logged_out=1 shows an
        # explicit signed-out screen (no auto-redirect), so the user stays in the CS app.
        self._redirect("/login?logged_out=1", {"Set-Cookie": auth.clear_cookie_header()})

    def log_message(self, *args):  # quiet console
        pass

    def do_HEAD(self):  # noqa: N802
        """Answer HEAD like GET but without a body. Prevents a 501 from the stdlib
        default handler for HEAD probes (load balancers, proxies, curl -I)."""
        # Reuse do_GET's logic but suppress the body: capture via a minimal shim.
        self._head_only = True
        try:
            self.do_GET()
        finally:
            self._head_only = False

    def do_GET(self):  # noqa: N802
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        query = urllib.parse.parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}
        try:
            # --- Auth routes (always available when auth is enabled) --------- #
            if auth.auth_required():
                if path == "/login":
                    self._handle_login(query); return
                if path == "/auth/callback":
                    self._handle_callback(query); return
                if path == "/logout":
                    self._handle_logout(); return

            principal = self._principal()
            engine.set_principal(principal)

            # /api/me is the UI's "who am I" — returns principal or unauthenticated.
            if path == "/api/me":
                if principal:
                    self._json(200, {"authenticated": True, "email": principal.get("email"),
                                     "name": principal.get("name"), "role": principal.get("role"),
                                     "owner_id": principal.get("owner_id")})
                else:
                    self._json(200, {"authenticated": False, "auth_required": auth.auth_required()})
                return

            # --- Auth gate: unauthenticated requests are turned away --------- #
            if auth.auth_required() and not principal:
                if path == "/" or not path.startswith("/api/"):
                    self._redirect("/login"); return
                self._json(401, {"error": "authentication required", "login": "/login"}); return

            if path == "/":
                if UI_PATH.exists():
                    self._send(200, UI_PATH.read_bytes(), "text/html; charset=utf-8")
                else:
                    self._send(200, b"<h1>CS Platform</h1><p>UI not found.</p>", "text/html")
                return
            # --- Tableau embedding (Connected App direct-trust SSO) --------- #
            # Config describes which dashboards to embed and where; it never returns
            # the connected-app secret. The JWT endpoint mints a short-lived embed
            # token for the AUTHENTICATED principal only — the Tableau identity is
            # the signed session's email, so a user cannot request a token for anyone
            # else. Both sit behind the auth gate above.
            if path == "/api/tableau/config":
                self._json(200, tableau.config_status()); return
            if path == "/api/tableau/jwt":
                if not tableau.is_configured():
                    self._json(503, {"error": "tableau_not_configured",
                                     "missing": tableau.config_status()["missing"]}); return
                username = (principal or {}).get("email") if principal else None
                if auth.auth_required() and not username:
                    self._json(401, {"error": "authentication required"}); return
                # When auth is disabled (legacy/local open mode) allow an explicit
                # override so the Reports page is still demonstrable; never a silent
                # real identity.
                username = username or os.environ.get("TABLEAU_DEV_USERNAME", "").strip()
                if not username:
                    self._json(400, {"error": "no_identity",
                                     "detail": "No authenticated email to use as the Tableau user."}); return
                try:
                    self._json(200, tableau.mint_jwt(username))
                except (RuntimeError, ValueError) as exc:
                    self._json(500, {"error": "jwt_mint_failed", "detail": str(exc)})
                return

            if path == "/api/portfolio":
                self._json(200, engine.portfolio()); return
            if path == "/api/roster":
                # Tier-1 whole-book roster (all customers, Managed vs Pooled/Scaled).
                # ?cohort=managed|pooled|all. Scoped to the principal (admin=all).
                cohort = (query.get("cohort") or ["all"])[0]
                cohort = cohort if cohort in ("managed", "pooled") else None
                self._json(200, engine.full_roster(cohort=cohort)); return
            if path == "/api/cohort/pooled":
                # Preview the 1-20 Agency + Corporate cohort (count + ARR + owners) that the
                # move-to-pooled action would touch. Owner-scoped inside the engine.
                self._json(200, engine.pooled_cohort()); return
            if path == "/api/csm/availability":
                # Live pooled CSM presence (Available/OOO) feeding the round-robin.
                self._json(200, engine.pooled_roster()); return
            if path == "/api/inbound/queue":
                # The persisted inbound queue (Option A channels -> triaged tickets).
                st = (query.get("status") or [None])[0]
                self._json(200, engine.inbound_queue(status=st)); return
            if path == "/api/strategic/review-queue":
                # Strategic monthly draft/review/approve queue (28th-31st window).
                period = (query.get("period") or [None])[0]
                self._json(200, engine.monthly_review_queue(period=period)); return
            if path == "/api/daily-brief":
                self._json(200, engine.daily_brief()); return
            if path == "/api/operating-rhythm":
                # Weekly time-blocked operating rhythm (WoW §3): the queue grouped into
                # Monday review / daily P1 risk / weekly renewals+expansion / weekly adoption.
                self._json(200, engine.operating_rhythm()); return
            if path == "/api/accounts":
                self._json(200, engine.portfolio()["accounts"]); return
            if path == "/api/tasks":
                self._json(200, engine.portfolio()["tasks"]); return
            if path == "/api/suppressed":
                self._json(200, engine.portfolio()["suppressed"]); return
            if path == "/api/revenue-motion":
                self._json(200, engine.revenue_motion()); return
            if path == "/api/expansion":
                self._json(200, engine.expansion_opportunities()); return
            if path == "/api/executive":
                self._json(200, engine.executive_summary()); return
            if path == "/api/churn-risk-matrix":
                # UC3 ML Churn Risk Matrix: the >=70% ML-churn cohort grouped by primary
                # risk driver with ARR impact. Owner-scoped inside the engine.
                self._json(200, engine.churn_risk_matrix()); return
            if path == "/api/onboarding-governance":
                # UC3 Rocket Lane implementation/onboarding governance (active projects,
                # time-in-onboarding, stalled-before-handoff, on-time handoff KPI).
                self._json(200, engine.onboarding_governance()); return
            if path == "/api/audit":
                self._json(200, _audit_read()); return
            if path == "/api/kpis":
                self._json(200, engine.kpis()); return
            if path == "/api/leaderboard":
                # V5 team performance leaderboard: per-CSM completion/outreach ranking +
                # weekly target-compliance KPI. Owner-scoped (admin all named; CSM self +
                # anonymised peers) inside the engine.
                self._json(200, engine.leaderboard()); return
            if path == "/api/task-events":
                self._json(200, {"events": list(engine._load_task_events().values())}); return
            if path == "/api/lifecycle":
                self._json(200, engine.lifecycle()); return
            if path == "/api/integrations":
                self._json(200, engine.integrations()); return
            if path == "/api/datagaps":
                self._json(200, engine.datagaps()); return
            if path == "/api/payment-risk":
                # Live Payment Risk Report (Stripe dunning + HubSpot billing/CSM + derived
                # JobAdder admin link), owner-scoped. Honest empty when Stripe not connected.
                self._json(200, engine.payment_risk_report()); return
            if path.startswith("/api/accounts/") and path.endswith("/digest"):
                account_id = path[len("/api/accounts/"):-len("/digest")].strip("/")
                try:
                    self._json(200, engine.monthly_digest(account_id)); return
                except engine.ForbiddenError:
                    self._json(403, {"error": "forbidden"}); return
                except KeyError:
                    self._json(404, {"error": "account not found"}); return
            if path == "/api/playbook":
                self._json(200, _playbook_summary()); return
            if path == "/api/agent/feedback/summary":
                self._json(200, _feedback_summary()); return
            if path.startswith("/api/accounts/") and path.endswith("/why-not"):
                account_id = path[len("/api/accounts/"):-len("/why-not")].strip("/")
                try:
                    self._json(200, engine.why_not(account_id))
                except engine.ForbiddenError:
                    self._json(403, {"error": "not your account", "account_id": account_id})
                except KeyError:
                    self._json(404, {"error": f"unknown account {account_id}"})
                return
            if path.startswith("/api/accounts/"):
                acct = path.rsplit("/", 1)[-1]
                try:
                    detail = engine.account_detail(acct)
                    detail["success_plans"] = _success_plans_for(acct)
                    self._json(200, detail)
                except engine.ForbiddenError:
                    self._json(403, {"error": "not your account", "account_id": acct})
                except KeyError:
                    self._json(404, {"error": f"unknown account {acct}"})
                return
            self._json(404, {"error": "not found", "path": path})
        except Exception as exc:  # noqa: BLE001
            self._json(500, {"error": f"{type(exc).__name__}: {exc}"})

    def do_POST(self):  # noqa: N802
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        try:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length) if length else b""
            ctype = self.headers.get("Content-Type", "")

            # Dev-login POST (local only; the HTML form submits form-encoded data).
            if path == "/auth/dev-login" and auth.dev_login_enabled() and not auth.cognito_configured():
                form = urllib.parse.parse_qs(raw.decode("utf-8", "replace"))
                self._handle_dev_login({"email": (form.get("email") or [""])[0]}); return

            body = json.loads(raw or b"{}") if raw else {}
            if not isinstance(body, dict):
                body = {}

            principal = self._principal()
            engine.set_principal(principal)
            if auth.auth_required() and not principal:
                self._json(401, {"error": "authentication required", "login": "/login"}); return
            # Progressive list enrichment: the UI posts the account ids currently visible
            # and gets back live-enriched row fields for them, so lists fill in the whole
            # book a screen at a time without the one-shot full-book fan-out.
            if path == "/api/accounts/enrich":
                ids = body.get("ids") if isinstance(body.get("ids"), list) else []
                self._json(200, engine.enrich_rows([str(i) for i in ids if i]))
                return
            # Scaled Tech-Touch inbound triage + round-robin (Use Case 1). Accepts a batch
            # of already-normalised inbound items and an optional roster of pooled CSMs
            # ({name, available}); returns classified/routed tickets with round-robin
            # assignment and 24h SLA. Deterministic and side-effect-free: a "technical ->
            # Zendesk" result is a handoff description, not an executed call. When no roster
            # is posted, derive available pooled CSMs from the portfolio owners.
            if path == "/api/inbound/triage":
                items = body.get("items") if isinstance(body.get("items"), list) else []
                roster = body.get("roster") if isinstance(body.get("roster"), list) else None
                if not roster:
                    # Live pooled roster with real availability (presence feed).
                    roster = engine.pooled_roster().get("roster", [])
                self._json(200, _inbound.triage_inbound(items, roster=roster))
                return
            if path == "/api/inbound/hubspot":
                # Option A intake seam: a HubSpot Service Hub ticket (or batch) lands here,
                # is normalised to the inbound-item shape, and runs through the same triage +
                # round-robin as /triage with the LIVE pooled roster. The actual HubSpot
                # Service Hub channel wiring + Service Hub Pro licence are RevOps config; this
                # endpoint is the platform-side contract that config posts to.
                raw = body.get("tickets") if isinstance(body.get("tickets"), list) else (
                    [body.get("ticket")] if isinstance(body.get("ticket"), dict) else
                    ([body] if (body.get("subject") or body.get("body") or body.get("from")) else []))
                VALID_CHANNELS = {"zendesk_misroute", "slack_call", "mailbox",
                                  "campaign_reply", "high_intent_form"}
                items = []
                for i, t in enumerate(raw):
                    if not isinstance(t, dict):
                        continue
                    ch = str(t.get("channel") or "mailbox").strip()
                    items.append({
                        "id": str(t.get("id") or t.get("ticket_id") or f"hs-{i}"),
                        "channel": ch if ch in VALID_CHANNELS else "mailbox",
                        "from": (t.get("from") or t.get("email") or "").strip(),
                        "subject": (t.get("subject") or "").strip(),
                        "body": (t.get("body") or t.get("message") or "").strip(),
                        "account_ref": (t.get("account_ref") or t.get("account_id") or None),
                        "received_at": t.get("received_at"),
                    })
                if not items:
                    self._json(400, {"error": "no tickets",
                                     "detail": "Post a HubSpot Service Hub ticket (channel, from, "
                                               "subject, body, account_ref, received_at) or a list in 'tickets'."})
                    return
                try:
                    record_audit("inbound_hubspot", principal,
                                 {"count": len(items), "channels": sorted({i["channel"] for i in items})})
                except Exception:  # noqa: BLE001
                    pass
                roster = engine.pooled_roster().get("roster", [])
                result = _inbound.triage_inbound(items, roster=roster)
                # Persist the routed tickets so the pooled team sees a live queue.
                try:
                    engine.record_inbound(result.get("tickets", []))
                except Exception:  # noqa: BLE001
                    pass
                self._json(200, result)
                return
            if path == "/api/inbound/resolve":
                tid = str(body.get("ticket_id") or body.get("id") or "").strip()
                status = (body.get("status") or "resolved").strip()
                try:
                    record_audit("inbound_resolve", principal, {"ticket_id": tid, "status": status})
                except Exception:  # noqa: BLE001
                    pass
                try:
                    self._json(200, engine.resolve_inbound(tid, status=status))
                except KeyError:
                    self._json(404, {"error": "ticket not found"})
                except ValueError as exc:
                    self._json(400, {"error": str(exc)})
                return
            if path == "/api/csm/availability":
                # Set a pooled CSM's Available/OOO status (the Help Desk presence feed).
                name = (body.get("name") or "").strip()
                available = bool(body.get("available", True))
                until = body.get("until")
                note = (body.get("note") or "").strip() or None
                try:
                    record_audit("csm_availability", principal,
                                 {"name": name, "available": available})
                except Exception:  # noqa: BLE001
                    pass
                try:
                    self._json(200, engine.set_csm_availability(name, available, until=until, note=note))
                except ValueError as exc:
                    self._json(400, {"error": str(exc)})
                return
            if path == "/api/agent":
                question = (body.get("question") or "").strip() or "What are my top CS actions today?"
                account_id = (body.get("account_id") or "").strip() or None
                csm_owner = (body.get("csm_owner") or "").strip() or None
                # Multi-turn memory: the panel sends prior turns so follow-ups have context.
                history = body.get("history") if isinstance(body.get("history"), list) else None
                if history:
                    # Trust only role/content, cap length to keep the prompt bounded.
                    history = [{"role": t.get("role"), "content": t.get("content")}
                               for t in history[-8:] if isinstance(t, dict)]
                plugin = Path(__file__).resolve().parents[1] / "plugins" / "cs-orchestrator"
                sys.path.insert(0, str(plugin))
                import agent_runner  # noqa: E402
                result = agent_runner.run(question, account_id=account_id,
                                          csm_owner=csm_owner, principal=principal,
                                          history=history)
                # Immutable audit: who asked, what generated the answer, sources read, when.
                try:
                    harness = result.get("harness", {}) if isinstance(result, dict) else {}
                    record_audit("agent_answer", principal, {
                        "question": question[:300],
                        "account_id": account_id,
                        "route": harness.get("route", "bedrock_agent"),
                        "data_sources_read": harness.get("live_sources", []),
                        "judge_verdict": (harness.get("queue_judge", {}) or {}).get("verdict"),
                        "ok": bool(result.get("ok")),
                        "action_count": len(result.get("actions", []) or []),
                    })
                except Exception:  # noqa: BLE001
                    pass
                self._json(200, result)
                return
            if path == "/api/agent/feedback":
                rating = body.get("rating")
                if rating not in ("helpful", "needs_correction"):
                    self._json(400, {"error": "rating must be helpful or needs_correction"})
                    return
                self._json(200, _record_agent_feedback(body))
                return
            if path == "/api/agent/feedback/summary":
                self._json(200, _feedback_summary())
                return
            if path == "/api/playbook/proposals":
                try:
                    self._json(201, _record_playbook_proposal(body))
                except ValueError as exc:
                    self._json(400, {"error": str(exc)})
                return
            if path == "/api/success-plans":
                try:
                    self._json(201, _record_success_plan(body, principal))
                except ValueError as exc:
                    self._json(400, {"error": str(exc)})
                return
            if path.startswith("/api/playbook/proposals/") and path.endswith("/review"):
                proposal_id = path[len("/api/playbook/proposals/"):-len("/review")].strip("/")
                try:
                    self._json(200, _record_playbook_review(proposal_id, body))
                except ValueError as exc:
                    self._json(400, {"error": str(exc)})
                return
            if path == "/api/tasks/status":
                try:
                    # Contact-role hard close-gate (WoW §5): a renewal or onboarding task may
                    # not be COMPLETED until the 3 required contact roles are tagged. The UI
                    # sends account_id + gate=renewal|onboarding when applicable.
                    gate = (body.get("gate") or "").strip().lower()
                    acct = (body.get("account_id") or "").strip()
                    if body.get("status") == "completed" and gate in ("renewal", "onboarding") and acct:
                        if not engine.can_write_account(acct):
                            self._json(403, {"error": "forbidden",
                                             "detail": "You can only complete tasks on accounts you own."})
                            return
                        missing = engine.required_roles_missing(acct)
                        if missing:
                            self._json(409, {"error": "contact_roles_required",
                                             "detail": "Tag the required contact roles before closing this "
                                                       + gate + " task.",
                                             "missing_roles": missing})
                            return
                    event = _record_task_event(body)
                    try:
                        record_audit("task_status_change", principal, {
                            "task_id": event.get("task_id"), "status": event.get("status")})
                    except Exception:  # noqa: BLE001
                        pass
                    self._json(200, event)
                except ValueError as exc:
                    self._json(400, {"error": str(exc)})
                return
            if path.startswith("/api/accounts/") and path.endswith("/writeback"):
                account_id = path[len("/api/accounts/"):-len("/writeback")].strip("/")
                apply_write = bool(body.get("apply", False))
                # Owner-scope: a CSM may only write back to accounts they own (admin: any).
                if not engine.can_write_account(account_id):
                    self._json(403, {"error": "forbidden",
                                     "detail": "You can only write to accounts you own."})
                    return
                try:
                    record_audit("hubspot_writeback", principal,
                                 {"account_id": account_id, "apply": apply_write})
                except Exception:  # noqa: BLE001
                    pass
                self._json(200, engine.writeback(account_id, apply=apply_write))
                return
            if path.startswith("/api/accounts/") and path.endswith("/enrol-sequence"):
                account_id = path[len("/api/accounts/"):-len("/enrol-sequence")].strip("/")
                sequence_id = (body.get("sequence_id") or "").strip()
                apply_write = bool(body.get("apply", False))
                if not engine.can_write_account(account_id):
                    self._json(403, {"error": "forbidden",
                                     "detail": "You can only enrol accounts you own."})
                    return
                try:
                    record_audit("sequence_enrolment", principal,
                                 {"account_id": account_id, "sequence_id": sequence_id,
                                  "apply": apply_write})
                except Exception:  # noqa: BLE001
                    pass
                try:
                    self._json(200, engine.enrol_sequence(account_id, sequence_id, apply=apply_write))
                except (KeyError, ValueError) as exc:
                    self._json(400, {"error": str(exc)})
                return
            if path.startswith("/api/accounts/") and path.endswith("/log-note"):
                account_id = path[len("/api/accounts/"):-len("/log-note")].strip("/")
                note = (body.get("note") or "").strip()
                apply_write = bool(body.get("apply", False))
                if not engine.can_write_account(account_id):
                    self._json(403, {"error": "forbidden",
                                     "detail": "You can only log notes on accounts you own."})
                    return
                try:
                    record_audit("log_note", principal,
                                 {"account_id": account_id, "apply": apply_write})
                except Exception:  # noqa: BLE001
                    pass
                try:
                    self._json(200, engine.log_note(account_id, note, apply=apply_write))
                except (KeyError, ValueError) as exc:
                    self._json(400, {"error": str(exc)})
                return
            if path.startswith("/api/accounts/") and path.endswith("/tag-contact-role"):
                account_id = path[len("/api/accounts/"):-len("/tag-contact-role")].strip("/")
                email = (body.get("contact_email") or "").strip()
                role = (body.get("role") or "").strip()
                apply_write = bool(body.get("apply", False))
                if not engine.can_write_account(account_id):
                    self._json(403, {"error": "forbidden",
                                     "detail": "You can only tag contacts on accounts you own."})
                    return
                try:
                    record_audit("tag_contact_role", principal,
                                 {"account_id": account_id, "role": role, "apply": apply_write})
                except Exception:  # noqa: BLE001
                    pass
                try:
                    self._json(200, engine.tag_contact_role(account_id, email, role, apply=apply_write))
                except (KeyError, ValueError) as exc:
                    self._json(400, {"error": str(exc)})
                return
            if path.startswith("/api/accounts/") and path.endswith("/send-digest"):
                account_id = path[len("/api/accounts/"):-len("/send-digest")].strip("/")
                apply_write = bool(body.get("apply", False))
                if not engine.can_write_account(account_id):
                    self._json(403, {"error": "forbidden",
                                     "detail": "You can only send digests for accounts you own."})
                    return
                try:
                    record_audit("send_digest", principal,
                                 {"account_id": account_id, "apply": apply_write})
                except Exception:  # noqa: BLE001
                    pass
                try:
                    self._json(200, engine.send_digest(account_id, apply=apply_write))
                except engine.ForbiddenError:
                    self._json(403, {"error": "forbidden"})
                except KeyError:
                    self._json(404, {"error": "account not found"})
                return
            if path == "/api/digests/run":
                # Batch monthly-digest run (the 1st-of-month scheduler target). Owner-scoped
                # inside the engine: iterates only accounts the principal can write, so an
                # admin/service run covers the book and a CSM run covers theirs. Honesty
                # gates are preserved per account (dry-run / no recipient / no provider).
                apply_write = bool(body.get("apply", False))
                try:
                    record_audit("run_monthly_digests", principal, {"apply": apply_write})
                except Exception:  # noqa: BLE001
                    pass
                try:
                    self._json(200, engine.run_monthly_digests(apply=apply_write))
                except Exception as exc:  # noqa: BLE001
                    self._json(500, {"error": str(exc)})
                return
            if path == "/api/cohort/move-to-pooled":
                # Move the 1-20 Agency + Corporate cohort (or an explicit account_ids list)
                # to the pooled structure in HubSpot. Owner-scoped per account inside the
                # engine; two-gated + dry-run default; audited. Reversible.
                apply_write = bool(body.get("apply", False))
                account_ids = body.get("account_ids")
                clear_owner = bool(body.get("clear_owner", True))
                pooled_team = (body.get("pooled_team") or "").strip() or None
                if account_ids is not None and not isinstance(account_ids, list):
                    self._json(400, {"error": "account_ids must be a list"}); return
                try:
                    record_audit("move_to_pooled", principal,
                                 {"apply": apply_write, "clear_owner": clear_owner,
                                  "pooled_team": pooled_team, "account_ids": account_ids})
                except Exception:  # noqa: BLE001
                    pass
                try:
                    self._json(200, engine.move_to_pooled(account_ids=account_ids,
                                                           clear_owner=clear_owner,
                                                           pooled_team=pooled_team,
                                                           apply=apply_write))
                except Exception as exc:  # noqa: BLE001
                    self._json(500, {"error": str(exc)})
                return
            if path == "/api/strategic/review-comment":
                account_id = (body.get("account_id") or "").strip()
                comment = body.get("comment") or ""
                period = (body.get("period") or "").strip() or None
                if not engine.can_write_account(account_id):
                    self._json(403, {"error": "forbidden",
                                     "detail": "You can only review accounts you own."}); return
                try:
                    record_audit("digest_review_comment", principal,
                                 {"account_id": account_id, "period": period})
                except Exception:  # noqa: BLE001
                    pass
                try:
                    self._json(200, engine.add_review_comment(account_id, comment, period=period))
                except (KeyError, ValueError) as exc:
                    self._json(400, {"error": str(exc)})
                return
            if path == "/api/strategic/approve":
                account_id = (body.get("account_id") or "").strip()
                period = (body.get("period") or "").strip() or None
                if not engine.can_write_account(account_id):
                    self._json(403, {"error": "forbidden",
                                     "detail": "You can only approve accounts you own."}); return
                try:
                    record_audit("digest_approve", principal,
                                 {"account_id": account_id, "period": period})
                except Exception:  # noqa: BLE001
                    pass
                self._json(200, engine.approve_digest(account_id, period=period))
                return
            if path == "/api/strategic/dispatch":
                # 1st-of-month dispatch: send approved, auto-baseline unreviewed. Gated.
                apply_write = bool(body.get("apply", False))
                period = (body.get("period") or "").strip() or None
                try:
                    record_audit("digest_dispatch", principal, {"apply": apply_write, "period": period})
                except Exception:  # noqa: BLE001
                    pass
                try:
                    self._json(200, engine.dispatch_reviewed_digests(period=period, apply=apply_write))
                except Exception as exc:  # noqa: BLE001
                    self._json(500, {"error": str(exc)})
                return
            if path.startswith("/api/accounts/") and path.endswith("/create-csql"):
                account_id = path[len("/api/accounts/"):-len("/create-csql")].strip("/")
                name = (body.get("name") or "").strip()
                amount = body.get("amount_usd")
                note = (body.get("note") or "").strip() or None
                apply_write = bool(body.get("apply", False))
                if not engine.can_write_account(account_id):
                    self._json(403, {"error": "forbidden",
                                     "detail": "You can only create deals on accounts you own."})
                    return
                try:
                    record_audit("create_csql", principal,
                                 {"account_id": account_id, "name": name, "apply": apply_write})
                except Exception:  # noqa: BLE001
                    pass
                try:
                    self._json(200, engine.create_csql(account_id, name, amount_usd=amount, note=note, apply=apply_write))
                except (KeyError, ValueError) as exc:
                    self._json(400, {"error": str(exc)})
                return
            if path == "/api/zendesk/reply":
                ticket_id = str(body.get("ticket_id") or "").strip()
                reply = (body.get("body") or "").strip()
                public = bool(body.get("public", True))
                apply_write = bool(body.get("apply", False))
                try:
                    record_audit("zendesk_reply", principal,
                                 {"ticket_id": ticket_id, "public": public, "apply": apply_write})
                except Exception:  # noqa: BLE001
                    pass
                try:
                    self._json(200, engine.zendesk_reply(ticket_id, reply, public=public, apply=apply_write))
                except engine.ForbiddenError as exc:
                    self._json(403, {"error": "forbidden", "detail": str(exc)})
                except (KeyError, ValueError) as exc:
                    self._json(400, {"error": str(exc)})
                return
            if path == "/api/zendesk/status":
                ticket_id = str(body.get("ticket_id") or "").strip()
                status = (body.get("status") or "solved").strip()
                comment = (body.get("comment") or "").strip() or None
                apply_write = bool(body.get("apply", False))
                try:
                    record_audit("zendesk_set_status", principal,
                                 {"ticket_id": ticket_id, "status": status, "apply": apply_write})
                except Exception:  # noqa: BLE001
                    pass
                try:
                    self._json(200, engine.zendesk_set_status(ticket_id, status=status, comment=comment, apply=apply_write))
                except engine.ForbiddenError as exc:
                    self._json(403, {"error": "forbidden", "detail": str(exc)})
                except (KeyError, ValueError) as exc:
                    self._json(400, {"error": str(exc)})
                return
            self._json(404, {"error": "not found", "path": path})
        except Exception as exc:  # noqa: BLE001
            self._json(500, {"error": f"{type(exc).__name__}: {exc}"})


def main() -> int:
    port = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else PORT
    ThreadingHTTPServer.allow_reuse_address = True
    try:
        srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    except OSError as exc:
        print(f"Could not bind port {port}: {exc}")
        print(f"  Another server may be running. Try a different port:  python3 platform/server.py 8790")
        print(f"  Or free it:  lsof -nP -iTCP:{port} -sTCP:LISTEN   then  kill <PID>")
        return 1
    print(f"CS Platform running: http://localhost:{port}")
    print(f"  API: http://localhost:{port}/api/portfolio")
    # Warm the live account cache in the background so the first request never waits on
    # the cold ~30s vendor fan-out (a slow origin response can make the edge time out).
    def _warm():
        try:
            engine.portfolio()
        except Exception:  # noqa: BLE001
            pass
    import threading as _thr
    _thr.Thread(target=_warm, daemon=True).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

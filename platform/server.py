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
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins" / "cs-orchestrator"))
from adapters import sources as _src  # noqa: E402

# Transient OIDC flow state (state -> code_verifier), in-process. Fine for a single
# server; a multi-instance deployment would use a shared store.
_OIDC_FLOWS: dict[str, str] = {}

# Local dev-login page (only served when AUTH_DEV_LOGIN=1 and Cognito is NOT
# configured). Lets you sign in as any email to exercise per-user scoping without
# a real IdP. Admin-ness comes from AUTH_ADMIN_EMAILS / CS_ADMIN_GROUPS.
_DEV_LOGIN_HTML = """<!doctype html><html><head><meta charset=utf-8>
<title>CS Platform — Sign in</title><meta name=viewport content="width=device-width,initial-scale=1">
<style>body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:#0f1420;color:#e8edf5;
display:flex;min-height:100vh;align-items:center;justify-content:center;margin:0}
.card{background:#171f2e;border:1px solid #263149;border-radius:16px;padding:36px;width:360px;box-shadow:0 20px 60px rgba(0,0,0,.4)}
h1{font-size:1.3em;margin:0 0 4px}.sub{color:#8aa;font-size:.85em;margin-bottom:22px}
label{display:block;font-size:.8em;color:#9ab;margin:14px 0 6px}
input{width:100%;box-sizing:border-box;padding:11px 13px;border-radius:9px;border:1px solid #2c3854;background:#0f1626;color:#e8edf5;font-size:1em}
button{width:100%;margin-top:20px;padding:12px;border:0;border-radius:9px;background:linear-gradient(135deg,#3b82f6,#7c3aed);color:#fff;font-weight:600;font-size:1em;cursor:pointer}
.dev{margin-top:16px;font-size:.75em;color:#7788aa;text-align:center}.apps{display:flex;gap:10px;margin-bottom:20px}
.app{flex:1;text-align:center;padding:10px;border:1px solid #2c3854;border-radius:9px;font-size:.8em;color:#9ab}
.app.on{border-color:#3b82f6;color:#cfe;background:#12203a}</style></head>
<body><form class=card method=POST action="/auth/dev-login">
<h1>CS Platform</h1><div class=sub>Customer Success · sign in to your book</div>
<div class=apps><div class="app on">CS Platform</div><div class=app>JA Observe</div></div>
<label>Work email</label><input name=email type=email placeholder="you@jobadder.com" autofocus required>
<button type=submit>Sign in</button>
<div class=dev>Dev login (AUTH_DEV_LOGIN). Production uses Okta via Cognito SSO.</div>
</form></body></html>"""

# Shown when a user authenticates successfully but is NOT entitled to the CS Platform
# (not in a CS admin or user group). Defense-in-depth behind the UI launcher.
_NOT_AUTHORISED_HTML = """<!doctype html><html><head><meta charset=utf-8>
<title>CS Platform — Access required</title><meta name=viewport content="width=device-width,initial-scale=1">
<style>body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:#0f1420;color:#e8edf5;
display:flex;min-height:100vh;align-items:center;justify-content:center;margin:0}
.card{background:#171f2e;border:1px solid #263149;border-radius:16px;padding:40px;max-width:440px;text-align:center}
h1{font-size:1.25em;margin:0 0 10px}p{color:#9ab;line-height:1.5;font-size:.9em}
a{color:#8ab4ff}</style></head>
<body><div class=card>
<h1>You don't have access to the CS Platform</h1>
<p>Your JobAdder sign-in worked, but your account isn't a member of a Customer Success
access group. If you believe you should have access, ask your CS lead or platform
admin to add you to the CS Platform group.</p>
<p><a href="https://observe.jobadder.cloud/portal">Back to applications</a></p>
</div></body></html>"""

UI_PATH = Path(__file__).resolve().parent / "ui" / "index.html"
PORT = int(os.environ.get("CS_PORT", "8787"))
FEEDBACK_LOG = Path(__file__).resolve().parents[1] / ".cs-agent-feedback.jsonl"
PLAYBOOK_PROPOSALS = Path(__file__).resolve().parents[1] / ".cs-playbook-proposals.jsonl"
PLAYBOOK_RULES = Path(__file__).resolve().parents[1] / "plugins" / "cs-orchestrator" / "skills" / "cs-playbook" / "SKILL.md"
TASK_EVENTS = Path(os.environ.get("CS_TASK_EVENTS_FILE", str(Path(__file__).resolve().parents[1] / ".cs-task-events.jsonl")))


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
    def _send(self, code: int, body: bytes, content_type: str, extra_headers: dict | None = None) -> None:
        try:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(body)))
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

    def _finish_login(self, principal_core: dict) -> None:
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
        self._redirect("/", {"Set-Cookie": auth.cookie_header(token)})

    def _handle_login(self) -> None:
        # Dev-login: no Cognito needed. Renders a tiny form that posts an email.
        if not auth.cognito_configured() and auth.dev_login_enabled():
            self._send(200, _DEV_LOGIN_HTML.encode("utf-8"), "text/html; charset=utf-8")
            return
        # OIDC: start Authorization Code + PKCE against Cognito.
        import secrets as _secrets
        verifier, challenge = auth.pkce_pair()
        state = _secrets.token_urlsafe(24)
        _OIDC_FLOWS[state] = verifier
        self._redirect(auth.authorize_url(self._redirect_uri(), state, challenge))

    def _handle_callback(self, query: dict) -> None:
        code = (query.get("code") or [None])[0]
        state = (query.get("state") or [None])[0]
        verifier = _OIDC_FLOWS.pop(state, None) if state else None
        if not code or not verifier:
            self._redirect("/login?error=invalid_state"); return
        try:
            tokens = auth.exchange_code(code, self._redirect_uri(), verifier)
            info = auth.fetch_userinfo(tokens["access_token"])
            self._finish_login(auth.principal_from_userinfo(info))
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
                    self._handle_login(); return
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
            if path == "/api/portfolio":
                self._json(200, engine.portfolio()); return
            if path == "/api/daily-brief":
                self._json(200, engine.daily_brief()); return
            if path == "/api/accounts":
                self._json(200, engine.portfolio()["accounts"]); return
            if path == "/api/tasks":
                self._json(200, engine.portfolio()["tasks"]); return
            if path == "/api/suppressed":
                self._json(200, engine.portfolio()["suppressed"]); return
            if path == "/api/kpis":
                self._json(200, engine.kpis()); return
            if path == "/api/task-events":
                self._json(200, {"events": list(engine._load_task_events().values())}); return
            if path == "/api/lifecycle":
                self._json(200, engine.lifecycle()); return
            if path == "/api/integrations":
                self._json(200, engine.integrations()); return
            if path == "/api/datagaps":
                self._json(200, engine.datagaps()); return
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
                    self._json(200, engine.account_detail(acct))
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
            if path == "/api/agent":
                question = (body.get("question") or "").strip() or "What are my top CS actions today?"
                account_id = (body.get("account_id") or "").strip() or None
                csm_owner = (body.get("csm_owner") or "").strip() or None
                plugin = Path(__file__).resolve().parents[1] / "plugins" / "cs-orchestrator"
                sys.path.insert(0, str(plugin))
                import agent_runner  # noqa: E402
                self._json(200, agent_runner.run(question, account_id=account_id, csm_owner=csm_owner))
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
            if path.startswith("/api/playbook/proposals/") and path.endswith("/review"):
                proposal_id = path[len("/api/playbook/proposals/"):-len("/review")].strip("/")
                try:
                    self._json(200, _record_playbook_review(proposal_id, body))
                except ValueError as exc:
                    self._json(400, {"error": str(exc)})
                return
            if path == "/api/tasks/status":
                try:
                    self._json(200, _record_task_event(body))
                except ValueError as exc:
                    self._json(400, {"error": str(exc)})
                return
            if path.startswith("/api/accounts/") and path.endswith("/writeback"):
                account_id = path[len("/api/accounts/"):-len("/writeback")].strip("/")
                self._json(200, engine.writeback(account_id, apply=bool(body.get("apply", False))))
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
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

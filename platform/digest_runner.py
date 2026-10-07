"""One-shot monthly-digest batch runner (the 1st-of-month scheduler target).

EventBridge Scheduler starts an ECS task from the SAME task definition as the web
service but overrides the container command to run this module. It therefore inherits
the identical task role and SSM secrets, so the vendor adapters are live exactly as
they are for the app, with no extra auth surface and no ALB exposure.

It runs as the SYSTEM principal (no CSM scope) so the batch covers the whole named-
account book. Every per-account honesty gate inside engine.run_monthly_digests is
preserved: writes-off / no recipient / no email provider never send. Until an outbound
email provider (CS_EMAIL_PROVIDER) is connected this run compiles and reports only.

Usage:
    python3 platform/digest_runner.py            # dry-run (compile + report only)
    python3 platform/digest_runner.py --apply    # gated send (still no-op without a provider)

Env:
    CS_DIGEST_APPLY=1   same as --apply (so the scheduler can set it via env)

The JSON summary is printed to stdout, which lands in the task's CloudWatch log group.
A non-zero exit is returned only on an unexpected failure, so a failed scheduled run is
visible in EventBridge's run history.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import engine  # noqa: E402


def main() -> int:
    apply = ("--apply" in sys.argv[1:]) or (
        str(os.environ.get("CS_DIGEST_APPLY", "")).lower() in ("1", "true", "yes", "on")
    )
    # System principal: no CSM owner-scope, so the batch spans the whole book. The write
    # gate (CS_ALLOW_WRITE) and the email-provider gate still apply per account.
    engine.set_principal(None)
    try:
        # Use the strategic review/approve WORKFLOW dispatch (UC2 1st-of-month cadence):
        # APPROVED reports send, still-DRAFT (unreviewed) accounts auto-send the baseline
        # (spec edge case), COMMENTED-but-not-approved are held. Review state is read from
        # the persistent JSONL store, so the comments/approvals CSMs made in the web app
        # (a different process) are honoured here. Honesty gates in send_digest preserved.
        result = engine.dispatch_reviewed_digests(apply=apply)
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"ok": False, "error": str(exc)}), flush=True)
        return 1
    s = result.get("summary", {})
    print(json.dumps({"ok": True, **result}, default=str), flush=True)
    # Human-readable one-liner for quick log scanning.
    print(
        f"digest dispatch {result.get('period')}: "
        f"named={s.get('named_accounts')} approved_dispatched={s.get('approved_dispatched')} "
        f"auto_baseline={s.get('auto_baseline')} held={s.get('held_commented')} "
        f"errors={s.get('errors')} apply={result.get('apply_requested')}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

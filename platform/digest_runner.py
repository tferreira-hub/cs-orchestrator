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
        result = engine.run_monthly_digests(apply=apply)
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"ok": False, "error": str(exc)}), flush=True)
        return 1
    s = result.get("summary", {})
    print(json.dumps({"ok": True, **result}, default=str), flush=True)
    # Human-readable one-liner for quick log scanning.
    print(
        f"digest run {result.get('period')}: in_scope={s.get('accounts_in_scope')} "
        f"compiled={s.get('compiled')} sent={s.get('sent')} dry_run={s.get('dry_run')} "
        f"missing_admin={s.get('missing_primary_admin')} "
        f"no_provider={s.get('no_email_provider')} errors={s.get('errors')} "
        f"apply={result.get('apply_requested')} provider={result.get('email_provider')}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

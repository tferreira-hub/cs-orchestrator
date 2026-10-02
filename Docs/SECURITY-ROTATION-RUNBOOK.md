# Security rotation runbook — Stripe key + Tableau connected-app secret

Status date: 2026-10. Owner actions (IT / Security / RevOps). The platform code is already
built for the secure end state; these are operational rotations, not code changes. Nothing
here should be executed without the new credentials in hand, because a bad rotation breaks
live reads (Payment Risk Report, Tableau embeds).

Secrets live in SSM SecureString under `/cs-platform/*` (see `infra/ssm.tf`). Terraform
owns the parameter *slots* with `lifecycle.ignore_changes = [value]`, so a rotated value is
NOT clobbered on the next `terraform apply`. Values are set out-of-band, never committed.

---

## 1. Stripe: replace the live secret key with a restricted read-only key

### Why
Verified 2026-10: the deployed `/cs-platform/sources/stripe-key` is an `sk_live_...` full
secret key, and the task runs with `CS_ALLOW_STRIPE_SECRET_KEY=1` to permit it. A full
secret key can create charges, refunds, and mutate every Stripe object. The CS Platform
only ever issues three **read** calls:

| Call | Stripe permission needed |
| --- | --- |
| `GET /v1/customers/search` | **Customers → Read** |
| `GET /v1/invoices` (open invoices) | **Invoices → Read** |
| `GET /v1/subscriptions` | **Subscriptions → Read** |

A restricted key (`rk_live_...`) scoped to exactly those three resources, read-only,
removes all write/refund blast radius while keeping the Payment Risk Report fully live.

### Steps (owner)
1. Stripe Dashboard → Developers → API keys → **Create restricted key**.
   - Name: `cs-platform-readonly`.
   - Permissions: **Customers = Read**, **Invoices = Read**, **Subscriptions = Read**.
     Everything else **None**. (Core resource `charges`/`payment_intents` are not needed;
     invoice status is sufficient for the dunning/at-risk signals.)
   - Create → copy the `rk_live_...` value.
2. Put it in SSM (replaces the slot value; Terraform will not clobber it):
   ```
   aws ssm put-parameter --name /cs-platform/sources/stripe-key \
     --type SecureString --overwrite --value "rk_live_..." \
     --profile <tooling-admin> --region ap-southeast-2
   ```
3. Drop the override so the `sk_live_` guard is active again. In `infra/variables.tf`
   `app_environment`, remove the `CS_ALLOW_STRIPE_SECRET_KEY = "1"` line (or set it to
   `"0"`), then `terraform apply` (infra workflow, `apply=true`). The adapter accepts
   `rk_live_` without the override; the guard will now REFUSE any future `sk_live_`.
4. Redeploy the service so the task picks up the new SSM value (new task revision). The
   execution role reads the secret at launch; a fresh deployment is required.
5. Verify: open the Payment Risk Report; it should still populate (customers/invoices/
   subscriptions all read). Confirm `test_stripe_refuses_sk_live_key` still passes.
6. Revoke the old `sk_live_` key in the Stripe Dashboard once the `rk_` key is confirmed
   working.

### Rollback
If reads break, the issue is almost always a missing read scope on the `rk_` key — add the
missing resource (Customers/Invoices/Subscriptions) in Stripe, no redeploy needed (same
key value). Only if the key value itself must change do you repeat steps 2 + 4.

---

## 2. Tableau: rotate the connected-app secret

### Why
`/cs-platform/tableau/ca-secret-value` is the signing key the platform uses to mint the
direct-trust JWT that embeds Tableau views. Rotating it on a schedule limits exposure if
the value ever leaked. Verified 2026-10: the slot is populated (not placeholder).

### Steps (owner)
1. Tableau Cloud → Settings → **Connected Apps** → the CS Platform app
   (client id `d7bd4c64-2f66-435a-83e4-719f498da78d`, secret id
   `83e41b22-e3e3-4dd1-8b5d-85b315507d02`).
2. **Generate a new secret** on that connected app. Tableau allows two secrets per app, so
   generate the new one FIRST (both valid) to avoid an embed outage.
3. Put the new secret value in SSM:
   ```
   aws ssm put-parameter --name /cs-platform/tableau/ca-secret-value \
     --type SecureString --overwrite --value "<new-secret>" \
     --profile <tooling-admin> --region ap-southeast-2
   ```
   If the new secret also has a new secret **id**, update `TABLEAU_CA_SECRET_ID` in
   `infra/variables.tf` `app_environment` and `terraform apply`.
4. Redeploy the service (new task revision) so the embed JWT is signed with the new secret.
5. Verify: open the Reports page; the Tableau views should render (the JWT is accepted).
6. **Delete the old secret** on the Tableau connected app once embeds are confirmed.

### Rollback
If embeds break, the old secret is still valid until step 6 — point `TABLEAU_CA_SECRET_ID`
back to the old id (and SSM back to the old value) and redeploy.

---

## General hygiene (not blocking)
- All `/cs-platform/*` secrets are SecureString with KMS at rest and are only readable by
  the task execution role (SSM resource policy in `infra/iam.tf`). No secret is in the repo.
- Rotate `AUTH_SECRET` (session HMAC) and the Cognito client secret on the same cadence;
  same SSM-put + redeploy pattern. Rotating `AUTH_SECRET` invalidates active sessions
  (users re-login), so do it in a low-traffic window.
- The HubSpot and Zendesk tokens are already least-privilege-ish (HubSpot private app
  scoped to the CRM objects used; Zendesk token is the Integrations agent). Review their
  scopes on the same cadence.

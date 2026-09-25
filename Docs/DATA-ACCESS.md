# Data access and live integration

The CS Platform UI runs on **live data only**. A source without credentials, or a live
lookup that fails, is marked `not connected` rather than being replaced with fixture data.
The standalone MCP server is also live-only by default; set `CS_MCP_MODE=fixture` only
for an explicit offline demo. No secret is ever stored in code or the repo.

## Read-only policy (enforced)
Every live API call is **read-only**. Enforcement is at the HTTP layer:
- `http_patch` is **hard-blocked** unless `CS_ALLOW_WRITE=1` (off by default).
- `http_post` is allowed **only for vendor search endpoints** (reads); any other POST is refused in read-only mode.
- HubSpot's bi-directional write-back (`push_cs_data`) runs as a **dry-run** by default: it reports what it *would* write and sends no PATCH.
- Stripe refuses a live secret key (`sk_live_`); use a **restricted read-only** key (`rk_...`). Override with `CS_ALLOW_STRIPE_SECRET_KEY=1` only if you understand the key's full scope.

To enable the real HubSpot push-back later: set `CS_ALLOW_WRITE=1` **and** use a token with `crm.objects.companies` write scope. Leave it unset for a purely read-only demo.

The platform workflow is `POST /api/accounts/{account_id}/writeback` with `{}` for a
dry-run audit result. It verifies the `cs_health_score`, `cs_risk_status`, and
`cs_active_playbook` properties and reports missing definitions without mutating
HubSpot. An actual apply requires both `CS_ALLOW_WRITE=1` and a request body of
`{"apply": true}`. Every response includes an `audit_id`, timestamp, mode, property
status, and mutation result.

## Canonical account identity
All sources join on the shard+tenant id **AUx-yyyyy** (for example `au1-12345`), stored as:
- HubSpot: company property `account_id`
- Stripe: customer `metadata.ja_account_id`
- Pendo: account id
- Zendesk: organization `external_id`
- Jiminny: account id

`adapters/identity.py` normalises `-` vs `_` and case, so `AU1_12345` and `au1-12345`
match. Instance type is derived from the id: no suffix = primary, a suffix like `-sbx`
= test, a different shard = secondary. This drives the multi-instance suppression hook.

## Environment variables (set the ROTATED keys, never ones shared in chat)
```bash
# HubSpot (read; write only if CS_ALLOW_WRITE=1)
export HUBSPOT_TOKEN=...

# Stripe (RESTRICTED read-only key, rk_..., NOT sk_live_)
export STRIPE_KEY=rk_live_...

# Pendo
export PENDO_KEY=...

# Zendesk
export ZENDESK_SUBDOMAIN=jobadder
export ZENDESK_EMAIL=integrations@jobadder.com
export ZENDESK_TOKEN=...

# Jiminny
export JIMINNY_KEY=...

# Redshift churn status (read-only)
export AWS_PROFILE=DataPlatform
export AWS_REGION=ap-southeast-2
export REDSHIFT_DATABASE=dwh
export REDSHIFT_WORKGROUP=data-platform-redshift-warehouse-wg-prod
export REDSHIFT_CHURN_TABLE=marts.int_ds_account_churn_scoring
export REDSHIFT_CHURN_ID_COLUMN=nk_ja_account
export REDSHIFT_CHURN_MODE=status
export REDSHIFT_CHURN_STATUS_COLUMN=calculated_churn_status

# Switches
export CS_USE_LIVE=1     # default on: any source with a key goes live
# export CS_ALLOW_WRITE=1  # leave UNSET for read-only (recommended)
```
Put these in a local `.env` (gitignored) or your shell. If no key is set for a source,
that source is shown as not connected. The explicit MCP offline mode is the only path
that reads fixtures.

## Security reminders
- Rotate any key that has been shared anywhere. Treat a Stripe `sk_live_` key as an
  emergency: revoke it in the Stripe dashboard immediately and reissue a restricted key.
- Prefer least-privilege, read-only keys everywhere.
- Keys live only in the environment, never in the repo (`.env`, `*.key`, `secrets.*` are gitignored).

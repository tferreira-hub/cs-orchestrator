# Data access and live integration

The CS Platform runs on **API-shaped fixtures by default** and switches any source to
**live data** when that source's credentials are present in the environment. No secret
is ever stored in code or the repo.

## Read-only policy (enforced)
Every live API call is **read-only**. Enforcement is at the HTTP layer:
- `http_patch` is **hard-blocked** unless `CS_ALLOW_WRITE=1` (off by default).
- `http_post` is allowed **only for vendor search endpoints** (reads); any other POST is refused in read-only mode.
- HubSpot's bi-directional write-back (`push_cs_data`) runs as a **dry-run** by default: it reports what it *would* write and sends no PATCH.
- Stripe refuses a live secret key (`sk_live_`); use a **restricted read-only** key (`rk_...`).

To enable the real HubSpot push-back later: set `CS_ALLOW_WRITE=1` **and** use a token with `crm.objects.companies` write scope. Leave it unset for a purely read-only demo.

## Canonical account identity
All sources join on the shard+tenant id **AUx-yyyyy** (for example `au1-12345`), stored as:
- HubSpot: company property `account_ref`
- Stripe: customer `metadata.account_ref`
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

# Optional churn model endpoint
export CHURN_API_URL=...
export CHURN_KEY=...

# Switches
export CS_USE_LIVE=1     # default on: any source with a key goes live
# export CS_ALLOW_WRITE=1  # leave UNSET for read-only (recommended)
```
Put these in a local `.env` (gitignored) or your shell. If no key is set for a source,
that source stays on fixtures. If a live call fails, it falls back to fixtures and logs
a warning, so the platform never breaks.

## Security reminders
- Rotate any key that has been shared anywhere. Treat a Stripe `sk_live_` key as an
  emergency: revoke it in the Stripe dashboard immediately and reissue a restricted key.
- Prefer least-privilege, read-only keys everywhere.
- Keys live only in the environment, never in the repo (`.env`, `*.key`, `secrets.*` are gitignored).

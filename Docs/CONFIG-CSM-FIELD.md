# Scope / filter by a HubSpot custom "CSM" field

The platform decides which CSM "owns" an account (for owner-scoping and the CSM filter)
from an **ordered list of HubSpot company fields**, taking the first non-empty value, then
falling back to the record owner (`hubspot_owner_id`). This is configurable — no code
change — via one environment variable.

## Config
Set `CS_CSM_FIELD` to a comma-separated list of HubSpot company property **internal names**,
most specific first:

```
CS_CSM_FIELD=customer_success_manager
# or an ordered fallback:
CS_CSM_FIELD=cs_lead_owner,retention_owner
# or go back to the record owner only:
CS_CSM_FIELD=hubspot_owner_id
```

- Default (unset): `retention_owner,customer_success_manager`.
- The configured field(s) are automatically added to the company search, so the value is
  fetched from HubSpot without any further change.
- `csm_source` on each account records which field actually drove the assignment (useful
  for auditing).

In this repo the value is set in Terraform: `infra/variables.tf` →
`app_environment.CS_CSM_FIELD` (then `terraform apply` + the normal deploy), or directly on
the ECS task definition's environment.

## Owner-field vs text-field (handled automatically)
The resolver detects the value type:

- **HubSpot user / owner field** (value is a numeric owner id, e.g. `12345`): populates
  `owner_id`; the display name resolves from the HubSpot owner record. This is the path
  that enables **owner-scoping** — the logged-in CSM's SSO email → owner id → matching
  accounts.
- **Custom text field** (value is a person's name, e.g. `Faizaa Khan`): populates
  `csm_name` directly and leaves `owner_id` empty, so no invalid `/owners/{name}` lookup is
  attempted. The CSM filter can match on the name.

> Recommendation: for live **owner-scoping** (each CSM auto-scoped to their book on login),
> use a HubSpot **user/owner-type** custom field so the value is an owner id that joins to
> the SSO identity. A text-name field supports **display and the CSM filter**, but can't be
> joined to a login identity unless we also map names → SSO emails.

## To finish wiring your field
Tell us the custom field's **internal name** (HubSpot → Settings → Properties → the field →
"Internal name") and its **type** (user/owner vs single-line text). Then it's a one-line
`CS_CSM_FIELD` change + deploy.

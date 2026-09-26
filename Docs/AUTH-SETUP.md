# CS Platform — Authentication & Access Setup

The CS Platform reuses the **existing JobAdder SSO chain** already powering JA Observe:

```
User → Cognito Hosted UI → SAML → AWS Identity Center → Corporate IdP (Okta)
```

No new user pool and no IdP change are required. Two things are needed for production,
plus one Identity Center group for the dedicated CS Admin role. Everything on the CS
Platform application side is already implemented and only reads configuration.

---

## 1. Cognito — add a CS Platform app client (DONE in Terraform)

Cognito is Terraform in **`ja-observe-api/infra/environments/{prod,devops}/auth.tf`**.
The CS Platform app client has been added on branch **`feat/cs-platform-cognito-client`**
as a second client in the *existing* `aws_cognito_user_pool.main` (shared pool = shared
SSO). It is named `${local.name_prefix}-cs-platform` and uses the `/auth/callback`
redirect path (this app is Python OIDC+PKCE, not NextAuth, so it does NOT use
`/api/auth/callback/cognito`). The change also adds:

- `variable "cs_platform_domain"` (default `cs.jobadder.cloud`)
- outputs `cs_platform_cognito_client_id` / `_secret` / `_issuer`
- SSM param `/cs-platform/auth/cognito-client-secret`

To activate: review the branch, `terraform plan`, and apply in each env. Retrieve config:

```bash
terraform output cs_platform_cognito_client_id
terraform output cs_platform_cognito_issuer
terraform output -raw cs_platform_cognito_client_secret   # or SSM /cs-platform/auth/cognito-client-secret
```

The shared hosted-UI domain prefix is **`ja-observe-auth`** (`${var.project}-auth`),
i.e. `https://ja-observe-auth.auth.<region>.amazoncognito.com`.

## 2. Identity Center — create the dedicated CS Admin group (Management account)

Identity Center groups are **not** managed by Terraform in these repos (they live in the
Management account and are referenced by id, as the existing `JA-Observe-*` groups are).
So this is a Console (or Management-account IaC) step:

1. In the **Management account** Identity Center → Groups → create **`CS-Platform-Admins`**.
2. Assign CS leadership / RevOps users to it.
3. Ensure the group is in scope of the SAML application that federates to the
   `ja-observe` Cognito pool (the `JobAdderSSO` provider), so it appears in the `Group`
   SAML claim. `auth.tf` already maps that claim → Cognito `custom:groups`; no Cognito or
   app change is needed.
4. Capture the group **ID** (Console shows it, e.g. `a4d8e4c8-...`) and set it as
   `CS_ADMIN_GROUPS` on the CS Platform (id is preferred over display name).

Until the group exists, use `AUTH_ADMIN_EMAILS` (break-glass) to grant admin.

## 3. CS Platform environment (application config)

Set these where the CS Platform server runs (already consumed by `platform/auth.py`
and `platform/rbac.py`):

```bash
AUTH_COGNITO_ISSUER=https://cognito-idp.<region>.amazonaws.com/<userPoolId>
AUTH_COGNITO_ID=<cs_platform_client_id>          # terraform output above
AUTH_COGNITO_SECRET=<client secret>              # from Cognito
AUTH_COGNITO_DOMAIN=ja-observe-auth              # existing shared hosted-UI domain
AUTH_SECRET=<openssl rand -base64 32>            # session-cookie signing secret
CS_SECURE_COOKIE=1                               # behind TLS/Cloudflare
CS_PUBLIC_URL=https://<cs-platform-domain>       # for the OIDC redirect_uri
CS_ADMIN_GROUPS=<CS-Platform-Admins group id>    # dedicated admin group (id preferred)
# Optional break-glass admins (works without any group), comma-separated:
# AUTH_ADMIN_EMAILS=lead@jobadder.com,revops@jobadder.com
```

**Security note:** with `CS_ADMIN_GROUPS` unset there is NO admin group — admin can then
only come from `AUTH_ADMIN_EMAILS`. A group is never admin unless its id/name is
explicitly listed. Everyone authenticated who is not an admin is a **scoped CSM** who
sees only the accounts they own (matched via HubSpot `hubspot_owner_id` ↔ SSO email).

## 4. Local development (no Cognito needed)

```bash
AUTH_DEV_LOGIN=1 AUTH_ADMIN_EMAILS=you@jobadder.com python3 platform/server.py
```

`/login` serves a simple email form; sign in as any email to exercise scoping. Dev-login
is automatically disabled once `AUTH_COGNITO_*` is configured.

## 5. Launcher on the JA Observe login page (DONE, optional to merge)

Added on branch **`feat/cs-platform-launcher`** in **`ja-observe-ui`**: the signed-out
screen (`/login?logged_out=true`) shows two app cards — "JA Observe" and "CS Platform".
Because both apps use the same Cognito pool, one sign-in covers both; each app still
completes its own OIDC callback. The CS Platform URL is configurable via
`NEXT_PUBLIC_CS_PLATFORM_URL` (default `https://cs.jobadder.cloud`). Cosmetic — merging
it is optional and independent of the SSO working.

## How access resolves (summary)

| Who | Role | Sees |
|-----|------|------|
| In `CS_ADMIN_GROUPS` or `AUTH_ADMIN_EMAILS` | admin | ALL accounts + portfolio-wide KPIs |
| Any other authenticated user | csm | ONLY the accounts they own (their book) |

## 7. DNS — making `cs.jobadder.cloud` resolve (required)

`cs.jobadder.cloud` is served by the **same ALB** as `observe.jobadder.cloud`, routed by
an ALB host-header rule (in `devops/cs-platform.tf`), and covered by the existing
`*.jobadder.cloud` wildcard cert — so **no new cert or load balancer is needed**. What is
needed is a DNS record, exactly like `observe.jobadder.cloud`:

- **Public record lives in Cloudflare** (not Terraform — same as `observe.jobadder.cloud`,
  see the note in `devops/dns.tf`). Add in the Cloudflare dashboard/API:
  - Type: `CNAME` (or A/AAAA), Name: `cs`, Target: the ALB hostname
    (`terraform output alb_dns_name` in devops), **Proxied (orange cloud)**, TLS **Full (Strict)**.
  This mirrors the existing `observe` record 1:1.
- **Internal alias** `cs.jobadder.devops` → ALB is Terraform-managed in `devops/dns.tf`
  (mirrors `observe.jobadder.devops`).

Until the Cloudflare record exists, `cs.jobadder.cloud` returns NXDOMAIN (an unresolved
domain / Cloudflare error page) — which is what a "wrong page" looks like; it is not the
CS Platform failing.

## 8. What the sign-in → CS Platform flow looks like

Both apps use the **same Cognito user pool**, so it is one SSO session:

1. From the JA Observe login launcher, clicking **CS Platform** opens `https://cs.jobadder.cloud`.
2. The CS Platform runs its **own** OIDC round-trip (`/login` → Cognito → `/auth/callback`).
   Because the Cognito session already exists (or is created once), the user is **not asked
   to log in a second time** — Cognito silently returns them.
3. They land on the **CS Platform dashboard**, scoped to their book (or all accounts if admin).

So "click CS Platform → after SSO, land on the CS page" is exactly the behaviour, once DNS
(§7) and the deployed service exist. The launcher card is only a link; the CS Platform
itself performs the redirect.

---

## 6. Deployment — how the CS Platform connects to its data sources

Two different auth mechanisms, so the deploy needs one IAM task role plus secrets:

| Source | Auth | IAM needed |
|--------|------|-----------|
| HubSpot, Zendesk, Stripe, Pendo, Jiminny, Entitlements | Vendor API key in header (env var, from SSM) | No — only `ssm:GetParameter` to read the secret |
| Cognito SSO login | OIDC HTTPS + app-client secret (from SSM) | No — only `ssm:GetParameter` |
| **Redshift churn** | **boto3 Redshift Data API (AWS creds)** | **Yes — task role, cross-account** |

### Redshift churn (cross-account)

The churn model lives in the **Data Platform account (`503561421603`)** and is read via the
Redshift Data API. The CS Platform runs in a different account, so its ECS **task role**
must assume a role in the Data Platform account. The adapter supports this via
`REDSHIFT_ASSUME_ROLE_ARN` (set it and the adapter does `sts:AssumeRole` → scoped Data API
client; unset = ambient creds for same-account/local use).

**Role in the Data Platform account** (call it `cs-platform-churn-reader`) — permissions:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "redshift-data:ExecuteStatement",
        "redshift-data:DescribeStatement",
        "redshift-data:GetStatementResult"
      ],
      "Resource": "*"
    },
    {
      "Effect": "Allow",
      "Action": ["redshift-serverless:GetCredentials"],
      "Resource": "arn:aws:redshift-serverless:<region>:503561421603:workgroup/<workgroup-id>"
    }
  ]
}
```

**Trust policy** on that role (allow the CS Platform task role to assume it):

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": { "AWS": "arn:aws:iam::<cs-platform-account>:role/<cs-platform-task-role>" },
    "Action": "sts:AssumeRole"
  }]
}
```

**CS Platform task role** — permissions: `sts:AssumeRole` on the reader role ARN, plus
`ssm:GetParameter` on `/cs-platform/auth/*` and the data-source secret parameters.

Then set on the CS Platform:
```
REDSHIFT_ASSUME_ROLE_ARN=arn:aws:iam::503561421603:role/cs-platform-churn-reader
```

**Graceful degradation:** if cross-account access is not yet arranged, the churn adapter
reports not-live and the platform falls back to the transparent computed-risk score
(Pendo/Zendesk/Stripe/Jiminny). Live ML churn can be enabled later without code change —
just set the env var once the role/trust exist.

> The CS Platform ECS service + task role (SSM read + cross-account `sts:AssumeRole`
> to the Data Platform churn reader) + host-based ALB routing are added in
> `ja-observe-api` PR #317 (stacked on the Cognito client PR #316), in the **`devops/`
> environment** — the active observe deployment (DevOps account `962430324941`); `prod/`
> is the pre-migration env. The cross-account `cs-platform-churn-reader` role is provided
> as `cs-platform-cross-account.tf.example` to apply in the Data Platform account
> (`503561421603`), trusting the CS Platform task role in the DevOps account. Remaining
> owner actions before apply: create the `CS-Platform-Admins` Identity Center group,
> populate the `/cs-platform/*` SSM parameters, build/push the container image
> (Dockerfile in cs-orchestrator) to the DevOps ECR, and point `cs.jobadder.cloud` DNS
> at the ALB.

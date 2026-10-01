# CS Platform — End-to-End Architecture

CS Platform is a **standalone tool** (separate from JA Observe) deployed in the
**Tooling account `350067031910`**, region `ap-southeast-2`, at
`https://csplatform.jobadder.tools`. This document shows how a request flows from the
browser all the way to live data, how it is deployed, and how it degrades gracefully.

See `infra/` for the Terraform and `infra/RUNBOOK.md` for the deploy steps.

---

## 1. Runtime request flow (edge → app → data)

```mermaid
flowchart TB
    user([CSM / Admin browser])

    subgraph CF[Cloudflare edge]
        dns["csplatform.jobadder.tools<br/>proxied · WAF · TLS"]
    end

    subgraph TOOL["AWS Tooling account 350067031910 · ap-southeast-2"]
        subgraph VPC["vpc-au-tooling 10.200.0.0/16"]
            subgraph PUB[Public subnets a/b/c]
                alb["ALB (internet-facing)<br/>SG: Cloudflare IPs only<br/>HTTPS 443 · *.jobadder.tools cert"]
            end
            subgraph PRIV[Private subnets a/b/c]
                task["ECS Fargate task<br/>cs-platform : 8787<br/>SG: ALB-only in · 443 out"]
            end
            nat["NAT gateway"]
        end

        cog["Cognito user pool<br/>cs-platform-auth (dedicated)<br/>hosted UI + app client"]
        ssm[("SSM /cs-platform/*<br/>SecureString secrets")]
        ecr[("ECR cs-platform")]
        logs[("CloudWatch /ecs/cs-platform")]
        bed["Bedrock<br/>claude-sonnet-4-5 (au)"]
    end

    idc["AWS Identity Center<br/>SAML app → Okta<br/>(Management acct)"]

    subgraph DP["Data Platform account 503561421603"]
        reader["cs-platform-churn-reader role"]
        rs["Redshift Serverless<br/>warehouse-wg-prod"]
    end

    vendors["Vendor APIs (HTTPS):<br/>HubSpot · Zendesk · Stripe<br/>Pendo · Jiminny · Rocket Lane"]

    user --> dns --> alb --> task
    task -->|OIDC + PKCE| cog
    cog -->|SAML| idc
    task -->|secrets at launch/runtime| ssm
    task -->|logs| logs
    task -->|InvokeModel| bed
    task -->|egress| nat
    nat --> vendors
    task -->|sts:AssumeRole| reader
    reader --> rs
    task -.pull image.-> ecr
```

**Security posture:** the ALB only accepts traffic from Cloudflare edge ranges, so it
cannot be reached directly; the Fargate task has no public IP and only the ALB SG can
reach port 8787; all task egress is TCP 443 via NAT; every secret comes from SSM, none
from code.

---

## 2. Authentication (dedicated SSO round-trip)

```mermaid
sequenceDiagram
    participant B as Browser
    participant S as CS Platform (Fargate)
    participant C as Cognito (cs-platform pool)
    participant I as Identity Center → Okta

    B->>S: GET / (no cs_session cookie)
    S->>B: 302 → Cognito authorize (PKCE challenge in signed cs_oidc_flow cookie)
    B->>C: /oauth2/authorize
    C->>I: SAML AuthnRequest
    I->>C: SAML assertion (Group claim)
    C->>B: 302 → /auth/callback?code
    B->>S: GET /auth/callback?code (+ flow cookie)
    S->>C: exchange code (client secret from SSM) + PKCE verifier
    C->>S: tokens → /oauth2/userInfo (email, custom:groups)
    S->>S: rbac.resolve_role() → admin | scoped CSM
    S->>B: Set signed __Secure- cs_session cookie → dashboard
```

Role is resolved from the SAML `Group` claim → `custom:groups`:
`CS-Platform-Admins` → **admin** (all accounts); anyone else → **scoped CSM** (own book
only, matched via HubSpot `hubspot_owner_id` ↔ SSO email). The role lives only in the
HMAC-signed cookie, never in client-editable data.

---

## 3. Deploy pipeline (CI/CD)

```mermaid
flowchart LR
    push([git push main]) --> gha[GitHub Actions]
    gha -->|OIDC assume<br/>ECR_PUSH_ROLE_ARN_TOOLING| role[Role in Tooling]
    role --> build[docker build + push<br/>ECR cs-platform:sha]
    build --> reg[register ECS task-def revision]
    reg --> upd[update service cs-platform<br/>rolling + circuit-breaker rollback]

    tf[Terraform] -.owns task-def SHAPE<br/>env/secrets/roles.-> svc[(ECS service)]
    upd -.swaps IMAGE only.-> svc
```

Terraform owns the task-definition **shape**; the pipeline owns the running **image**.
The service has `ignore_changes = [task_definition, desired_count]` so the two never
fight.

---

## 4. Graceful degradation (what happens if a dependency isn't wired yet)

| Missing dependency | Behaviour (no crash) |
|--------------------|----------------------|
| Cloudflare DNS record | Domain NXDOMAINs — DNS issue, not the app |
| Identity Center SAML app (`saml_metadata_url=""`) | Pool created without federation; use dev-login path |
| `cs-platform-churn-reader` trust | Churn adapter not-live → transparent computed-risk fallback |
| A vendor API key in SSM | That source reported as a **data gap**, never faked |
| Bedrock access | Agent path unavailable; deterministic dashboard queue still works |

---

## 5. Account / resource summary

| Concern | Resource |
|---------|----------|
| Account | Tooling `350067031910`, `ap-southeast-2` |
| Network | `vpc-0868ddde5e0399b31`, 3 public + 3 private subnets, 1 NAT |
| Domain / TLS | `csplatform.jobadder.tools` (Cloudflare) · `*.jobadder.tools` ACM cert |
| SSO | Dedicated Cognito pool `cs-platform-auth` + `csplatform-jobadder` hosted UI |
| Compute | ECS Fargate cluster + service `cs-platform`, ECR `cs-platform` |
| Secrets | SSM `/cs-platform/*` (8 SecureString params) |
| Churn (cross-acct) | Data Platform `503561421603` · `warehouse-wg-prod` via assumed role |
| LLM | Bedrock `au.anthropic.claude-sonnet-4-5` |

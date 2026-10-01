# CS Platform — Standalone Deployment Runbook

CS Platform is a **completely separate tool from JA Observe**. This stack deploys it
into its own dedicated footprint in the **Tooling account (`350067031910`)** with its
own Cognito/SSO, ALB, ECR, ECS cluster, IAM roles, SSM secrets, and the
`csplatform.jobadder.tools` domain. Nothing here is shared with the `ja-observe-*`
resources in the DevOps account (`880082283556`).

The Terraform lives in `infra/`.

---

## What Terraform creates (verified: `plan` = 31 to add, 0 change, 0 destroy)

| Area | Resources |
|------|-----------|
| **SSO** | Dedicated Cognito user pool `cs-platform-auth`, hosted-UI domain `csplatform-jobadder`, app client (OIDC+PKCE, confidential), optional SAML IdP `JobAdderSSO` |
| **Network** | ALB security group (Cloudflare-only ingress), service SG (ALB-only ingress, 443-only egress) — uses the **existing** Tooling VPC/subnets/NAT (data sources) |
| **Edge** | Internet-facing ALB, HTTPS listener (443, existing `*.jobadder.tools` cert), HTTP→HTTPS redirect, IP target group with `/login` health check |
| **Compute** | ECR repo `cs-platform` (immutable, scan-on-push, lifecycle), ECS Fargate cluster `cs-platform`, task definition, service (circuit-breaker rollback) |
| **IAM** | Execution role (ECR pull, logs, SSM read), task role (cross-account `sts:AssumeRole` to churn reader, `bedrock:InvokeModel`, SSM read) |
| **Config** | 8 SSM SecureString slots under `/cs-platform/*`, CloudWatch log group `/ecs/cs-platform` (30-day retention) |

---

## Deployment model: everything via pipeline

Infra is deployed by the **`infra` GitHub Actions workflow** (`.github/workflows/infra.yml`),
which runs `terraform plan` on PRs and `terraform apply` on `main` (or manual dispatch
with `apply=true`). The separate **`build-and-deploy`** workflow then owns the running
image. Nobody applies Terraform from a laptop in steady state. Cloudflare DNS is the only
manual step (by design).

### One-time bootstrap (all IaC, no laptop `terraform apply`)

The `infra` pipeline authenticates with the `cs-platform-tf-deploy` role, but Terraform
cannot create the role it authenticates with. We solve this declaratively: the role is
defined in a **CloudFormation template** (`infra/bootstrap/tf-oidc-bootstrap.yaml`) and
deployed once. CloudFormation is itself IaC, so there is no imperative apply from a
laptop and nothing clicked in the console.

```bash
aws cloudformation deploy \
  --template-file infra/bootstrap/tf-oidc-bootstrap.yaml \
  --stack-name cs-platform-tf-bootstrap \
  --capabilities CAPABILITY_NAMED_IAM \
  --region ap-southeast-2 \
  --profile Tooling.JA-Admin

# Wire the pipeline secret from the stack output:
ARN=$(aws cloudformation describe-stacks --stack-name cs-platform-tf-bootstrap \
  --region ap-southeast-2 --profile Tooling.JA-Admin \
  --query "Stacks[0].Outputs[?OutputKey=='TfDeployRoleArn'].OutputValue" --output text)
gh secret set TF_DEPLOY_ROLE_ARN_TOOLING --repo JobAdder/cs-orchestrator --body "$ARN"
```

From here everything is the Terraform `infra` pipeline. Terraform references this role as
a data source (`infra/tf_deploy_role.tf`) so its ARN also appears as a stack output. The
`ECR_PUSH_ROLE_ARN_TOOLING` app-deploy secret is created BY Terraform; set it from the
`infra` pipeline output after the first apply:

```bash
gh secret set ECR_PUSH_ROLE_ARN_TOOLING --repo JobAdder/cs-orchestrator \
  --body "$(terraform -chdir=infra output -raw github_deploy_role_arn)"
```

Order of operations: (1) deploy the CloudFormation bootstrap stack, (2) set
`TF_DEPLOY_ROLE_ARN_TOOLING`, (3) open a PR / merge to `main` so the `infra` pipeline
plans then applies the 33 Terraform resources, (4) set `ECR_PUSH_ROLE_ARN_TOOLING` from
the output. After that, infra changes flow through PR (plan) → merge (apply).

> Prerequisite confirmed live: state key `cs-platform/terraform.tfstate` in
> `au.terraform-state.tooling.jobadder` is free, and no pre-existing `cs-platform`
> cluster/ECR exist in the account.

## Post-apply — populate secrets (NEVER commit these)

Terraform creates the SSM slots with a placeholder. Set the real values out-of-band:

```bash
P=Tooling.JA-Admin; R=ap-southeast-2
aws ssm put-parameter --profile $P --region $R --overwrite --type SecureString \
  --name /cs-platform/auth/session-secret        --value "$(openssl rand -base64 32)"
aws ssm put-parameter --profile $P --region $R --overwrite --type SecureString \
  --name /cs-platform/auth/cognito-client-secret --value "<terraform output cognito_client_secret, see below>"
aws ssm put-parameter --profile $P --region $R --overwrite --type SecureString \
  --name /cs-platform/sources/hubspot-token      --value "<hubspot token>"
aws ssm put-parameter --profile $P --region $R --overwrite --type SecureString \
  --name /cs-platform/sources/stripe-key         --value "<rk_live_... restricted key>"
aws ssm put-parameter --profile $P --region $R --overwrite --type SecureString \
  --name /cs-platform/sources/pendo-key          --value "<pendo key>"
aws ssm put-parameter --profile $P --region $R --overwrite --type SecureString \
  --name /cs-platform/sources/zendesk-token      --value "<zendesk token>"
aws ssm put-parameter --profile $P --region $R --overwrite --type SecureString \
  --name /cs-platform/sources/rocket-lane-key    --value "<ROCKET_LANE_KEY>"
aws ssm put-parameter --profile $P --region $R --overwrite --type SecureString \
  --name /cs-platform/sources/jiminny-key        --value "<JIMINNY_KEY>"
```

The Cognito app-client secret is generated by Terraform; retrieve it with
`terraform output cognito_client_id` and read the secret from the Cognito console (or
`aws cognito-idp describe-user-pool-client`), then store it in the SSM slot above.

> **Rocket Lane / Jiminny:** the two keys shared during setup go into
> `/cs-platform/sources/rocket-lane-key` and `/cs-platform/sources/jiminny-key`
> respectively. They are **not** stored in git. Because they were transmitted in
> plaintext, **rotate both keys** and store the rotated values here.
> Confirm `ROCKET_LANE_API_URL` (set in `variables.tf → app_environment`) matches the
> JobAdder Rocket Lane tenant base URL before go-live.

## Build & push the first image

The GitHub Actions workflow (`.github/workflows/build-and-deploy.yml`) now targets the
Tooling ECR/cluster/service. It needs the repo secret `ECR_PUSH_ROLE_ARN_TOOLING`. **This
role is now codified** in `infra/github_oidc.tf` (scoped to the `main` branch of
`JobAdder/cs-orchestrator`, least-privilege ECR push + ECS deploy + PassRole on the task
roles) and created by `terraform apply`. The GitHub OIDC provider already exists in the
account, so no provider setup is needed. After apply, set the secret from the output:

```bash
gh secret set ECR_PUSH_ROLE_ARN_TOOLING \
  --repo JobAdder/cs-orchestrator \
  --body "$(terraform -chdir=infra output -raw github_deploy_role_arn)"
```

Push to `main` (or run `workflow_dispatch`) to build and deploy.

For a manual first push:
```bash
AWS_PROFILE=Tooling.JA-Admin aws ecr get-login-password --region ap-southeast-2 \
  | docker login --username AWS --password-stdin 350067031910.dkr.ecr.ap-southeast-2.amazonaws.com
docker build -t 350067031910.dkr.ecr.ap-southeast-2.amazonaws.com/cs-platform:<sha> .
docker push 350067031910.dkr.ecr.ap-southeast-2.amazonaws.com/cs-platform:<sha>
```

## DNS — make `csplatform.jobadder.tools` resolve (Cloudflare)

The hostname is Cloudflare-proxied; the ALB only accepts traffic from Cloudflare edge
ranges (see `cloudflare_ipv4_cidrs`/`cloudflare_ipv6_cidrs` in `variables.tf`). Add in
the **Cloudflare dashboard**:

- Type `CNAME`, Name `csplatform`, Target `terraform output alb_dns_name`,
  **Proxied (orange cloud)**, TLS mode **Full (Strict)**.

Until the record exists, the domain returns NXDOMAIN — that is DNS, not the app failing.

## Dedicated SSO (AWS Identity Center — Management account)

CS Platform now uses its **own** Cognito pool and its **own** SAML federation:

1. In the **Management account** Identity Center, create a **dedicated CS Platform SAML
   application** (separate from the JA Observe one). Its ACS URL / entity id are the
   Cognito pool's (`cognito_hosted_ui` output + `/saml2/idpresponse`).
2. Add a `Group` attribute to the SAML assertion.
3. Create the **`CS-Platform-Admins`** group, assign CS leadership, grant the SAML app.
4. Set `saml_metadata_url` in `terraform.tfvars` to the IdC app metadata URL and
   re-apply — Terraform then creates the `JobAdderSSO` SAML IdP and points the app
   client at it.

Until the SAML app exists, leave `saml_metadata_url = ""`; the pool is created without
federation and you can validate via the app's dev-login path.

## Cross-account churn (Data Platform account `503561421603`)

The task role assumes `cs-platform-churn-reader` in the Data Platform account for live
ML churn via the Redshift Data API. **This role is now codified** as
`infra/data-platform/cs-platform-churn-reader.tf.example` (a separate stack because it
targets a different account). Verified live: the role does **not** yet exist, and the
target workgroup `data-platform-redshift-warehouse-wg-prod` **does**.

To apply it (with Data Platform credentials):

```bash
cd infra/data-platform
cp cs-platform-churn-reader.tf.example main.tf
# set the real workgroup ARN:
aws --profile DataPlatform redshift-serverless get-workgroup \
  --workgroup-name data-platform-redshift-warehouse-wg-prod \
  --query workgroup.workgroupArn --output text
# edit var redshift_workgroup_arn default (or pass -var), then:
AWS_PROFILE=DataPlatform terraform init && AWS_PROFILE=DataPlatform terraform apply
```

The example's trust policy already allows the CS Platform task role
(`arn:aws:iam::350067031910:role/cs-platform-task`, confirm with
`terraform -chdir=infra output -raw task_role_arn`). If this is not yet applied, the
churn adapter reports not-live and the platform falls back to the computed risk score
(no code change needed).

## Verify

```bash
AWS_PROFILE=Tooling.JA-Admin aws ecs describe-services \
  --cluster cs-platform --services cs-platform \
  --query 'services[0].{running:runningCount,desired:desiredCount,status:status}'
```

Then browse to `https://csplatform.jobadder.tools` once DNS resolves — you should get the
CS Platform `/login` and complete the dedicated-pool SSO round-trip.

---

## Decommissioning the old DevOps deployment

The previous deployment in DevOps (`880082283556`) — cluster `ja-observe-devops`,
service `ja-observe-devops-cs-platform` — is currently scaled to **`desiredCount: 0`**
(already not running). Once the Tooling deployment is verified, remove that service,
its target group/listener rule, the `/cs-platform/*` SSM params, and the shared-pool
app client in the DevOps/ja-observe Terraform repo. Keep it at 0 until cutover is done.

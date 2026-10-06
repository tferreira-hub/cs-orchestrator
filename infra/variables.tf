# ---------------------------------------------------------------------------
# Core
# ---------------------------------------------------------------------------
variable "account_id" {
  description = "Target AWS account (Tooling). Enforced via provider allowed_account_ids."
  type        = string
  default     = "350067031910"
}

variable "region" {
  description = "AWS region."
  type        = string
  default     = "ap-southeast-2"
}

variable "environment" {
  description = "Environment tag/name suffix."
  type        = string
  default     = "prod"
}

variable "name_prefix" {
  description = "Prefix for all resource names. CS Platform is standalone (NOT ja-observe-*)."
  type        = string
  default     = "cs-platform"
}

# --- Mandatory JobAdder tag taxonomy -----------------------------------------
variable "tag_owner" {
  description = "Owner: accountable team/function (e.g. cloud-engineering, shared, rock)."
  type        = string
  default     = "cloud-engineering"
}

variable "tag_repository" {
  description = "Repository: where the code lives (comma-separated if multiple)."
  type        = string
  default     = "cs-orchestrator"
}

variable "tag_application" {
  description = "Application: the workload/service the resource supports."
  type        = string
  default     = "cs-platform"
}

variable "tag_ja_instance" {
  description = "JAInstance (optional): instance served, e.g. au1. Empty if not instance-specific."
  type        = string
  default     = ""
}

variable "tag_lifecycle" {
  description = "Lifecycle: persistent or temporary."
  type        = string
  default     = "persistent"
}

variable "tag_lifecycle_end_date" {
  description = "LifecycleEndDate (required only if lifecycle=temporary), YYYY-MM-DD."
  type        = string
  default     = ""
}

# ---------------------------------------------------------------------------
# Networking (existing Tooling VPC — discovered, not created here)
# ---------------------------------------------------------------------------
variable "vpc_id" {
  description = "Existing Tooling VPC id."
  type        = string
  default     = "vpc-0868ddde5e0399b31"
}

variable "public_subnet_ids" {
  description = "Public subnets for the internet-facing ALB (one per AZ)."
  type        = list(string)
  default = [
    "subnet-0b524de7f2881fb36", # public0 / ap-southeast-2a
    "subnet-0887ba91876db097a", # public1 / ap-southeast-2b
    "subnet-0928925b3f81d554a", # public2 / ap-southeast-2c
  ]
}

variable "private_subnet_ids" {
  description = "Private subnets for the Fargate tasks (egress via NAT to ECR/vendors)."
  type        = list(string)
  default = [
    "subnet-0ef572ff2a7997da8", # private0 / ap-southeast-2a
    "subnet-0d7029627ea5a09ee", # private1 / ap-southeast-2b
    "subnet-00a7ace2fc6978ae8", # private2 / ap-southeast-2c
  ]
}

# ---------------------------------------------------------------------------
# DNS / TLS
# ---------------------------------------------------------------------------
variable "domain_name" {
  description = "Public hostname served by the ALB (DNS record created in Cloudflare, not here)."
  type        = string
  default     = "csplatform.jobadder.tools"
}

variable "acm_certificate_arn" {
  description = "ACM cert for the ALB HTTPS listener. The existing *.jobadder.tools ISSUED cert."
  type        = string
  default     = "arn:aws:acm:ap-southeast-2:350067031910:certificate/6ec15a57-af0d-40cd-8082-8eb6761467bc"
}

# The domain is Cloudflare-proxied (orange cloud). The ALB must only accept traffic
# from Cloudflare's edge, NOT the whole internet — otherwise the ALB is directly
# reachable and Cloudflare's WAF/DDoS/TLS layer can be bypassed. Source of truth:
# https://www.cloudflare.com/ips/ (keep in sync; Cloudflare publishes changes rarely).
variable "cloudflare_ipv4_cidrs" {
  description = "Cloudflare edge IPv4 ranges allowed to reach the ALB."
  type        = list(string)
  default = [
    "173.245.48.0/20", "103.21.244.0/22", "103.22.200.0/22", "103.31.4.0/22",
    "141.101.64.0/18", "108.162.192.0/18", "190.93.240.0/20", "188.114.96.0/20",
    "197.234.240.0/22", "198.41.128.0/17", "162.158.0.0/15", "104.16.0.0/13",
    "104.24.0.0/14", "172.64.0.0/13", "131.0.72.0/22",
  ]
}

variable "cloudflare_ipv6_cidrs" {
  description = "Cloudflare edge IPv6 ranges allowed to reach the ALB."
  type        = list(string)
  default = [
    "2400:cb00::/32", "2606:4700::/32", "2803:f800::/32", "2405:b500::/32",
    "2405:8100::/32", "2a06:98c0::/29", "2c0f:f248::/32",
  ]
}

# ---------------------------------------------------------------------------
# Container / service sizing
# ---------------------------------------------------------------------------
variable "container_image" {
  description = "Full image ref deployed by Terraform on first apply. The CI pipeline swaps the image on later revisions (task_definition is ignored on the service)."
  type        = string
  default     = ""
}

variable "container_port" {
  description = "App listen port."
  type        = number
  default     = 8787
}

variable "task_cpu" {
  description = "Fargate task CPU units."
  type        = number
  default     = 512
}

variable "task_memory" {
  description = "Fargate task memory (MiB)."
  type        = number
  default     = 1024
}

variable "desired_count" {
  description = "Number of running tasks."
  type        = number
  default     = 1
}

# ---------------------------------------------------------------------------
# Cognito / SSO — DEDICATED pool (not the ja-observe shared pool)
# ---------------------------------------------------------------------------
variable "cognito_domain_prefix" {
  description = "Hosted-UI domain prefix: https://<prefix>.auth.<region>.amazoncognito.com"
  type        = string
  default     = "csplatform-jobadder"
}

variable "saml_metadata_url" {
  description = "AWS Identity Center SAML app metadata URL for the dedicated CS Platform SAML app. Leave empty to provision the pool without federation (dev/email login only) until the Identity Center app exists."
  type        = string
  default     = ""
}

variable "admin_group_name" {
  description = "Display name of the CS admin SSO group (for reference/docs)."
  type        = string
  default     = "CS-Platform-Admins"
}

# Identity Center emits group IDs (UUIDs), not names, in the SAML Group claim, so
# CS_ADMIN_GROUPS / CS_USER_GROUPS must be the group IDs. rbac.py matches by substring.
variable "cs_admin_group_ids" {
  description = "Identity Center group ID(s) that grant CS Platform admin (comma-separated). Members see all accounts."
  type        = string
  default     = ""
}

variable "cs_user_group_ids" {
  description = "Identity Center group ID(s) that grant scoped-CSM access (comma-separated). This is the hard entry gate; a user not in an admin or user group is denied. Admin groups also grant access."
  type        = string
  default     = ""
}

variable "auth_admin_emails" {
  description = "Break-glass admin emails (comma-separated). Grants access + admin without a group, for cutover/bootstrap before the Identity Center group IDs are wired. Keep short-lived."
  type        = string
  default     = ""
}

# ---------------------------------------------------------------------------
# Application configuration (non-secret env). Secrets come from SSM (see ssm.tf).
# ---------------------------------------------------------------------------
variable "app_environment" {
  description = "Non-secret environment variables injected into the task definition."
  type        = map(string)
  default = {
    AWS_REGION          = "ap-southeast-2"
    CS_SECURE_COOKIE    = "1"
    CS_PENDO_ACTIVITY   = "1"
    CS_STALE_REVALIDATE = "1"
    CS_CACHE_TTL        = "600"
    # Enriched/health-scored roster size. This is how many accounts get the full
    # multi-vendor signal fan-out + a computable health score per refresh (the deeply
    # scored slice behind the dashboard health mix). Unset defaults to 25 in code, which
    # left ~1,276 of the book "not scored yet"; 150 widens real coverage while keeping the
    # per-refresh fan-out bounded (each account = several live vendor calls, run with
    # CS_FETCH_WORKERS concurrency). Raise further only with an eye on vendor rate limits.
    CS_ROSTER_LIMIT = "150"
    # Stripe is accessed with the live SECRET key currently in SSM; permit it until a
    # restricted read-only key (rk_...) is issued. Needed by the Payment Risk Report and
    # any live dunning signals. SECURITY: prefer rotating STRIPE_KEY to an rk_ key and
    # removing this override.
    CS_ALLOW_STRIPE_SECRET_KEY = "1"
    # Enable the gated write framework so CSMs can act in-platform (HubSpot CS write-back,
    # sequence enrolment, and the reversible CRM writes added incrementally). Every write is
    # STILL gated per request: it requires an explicit apply=true, is owner-scoped (a CSM can
    # only write accounts they own), and is recorded in the immutable audit log. Nothing is
    # written autonomously.
    CS_ALLOW_WRITE               = "1"
    CS_BEDROCK_REGION            = "ap-southeast-2"
    CS_BEDROCK_MODEL             = "au.anthropic.claude-sonnet-4-5-20250929-v1:0"
    ZENDESK_SUBDOMAIN            = "jobadder"
    ZENDESK_EMAIL                = "integrations@jobadder.com"
    JIMINNY_REGION               = "eu"
    REDSHIFT_DATABASE            = "dwh"
    REDSHIFT_WORKGROUP           = "data-platform-redshift-warehouse-wg-prod"
    REDSHIFT_CHURN_TABLE         = "marts.int_ds_account_churn_scoring"
    REDSHIFT_CHURN_ID_COLUMN     = "nk_ja_account"
    REDSHIFT_CHURN_MODE          = "status"
    REDSHIFT_CHURN_STATUS_COLUMN = "calculated_churn_status"
    REDSHIFT_METRICS_TABLE       = "rpt.rpt_account_ndr_monthly"
    REDSHIFT_METRICS_ID_COLUMN   = "ja_account"
    # Rocket Lane onboarding connector (non-secret config; key is an SSM secret).
    # Confirm the exact base URL for the JobAdder Rocket Lane tenant before go-live.
    ROCKET_LANE_API_URL = "https://api.rocketlane.com/api"
    # Tableau embedding (Connected App direct trust). Non-secret config; the signing
    # secret (TABLEAU_CA_SECRET_VALUE) is an SSM SecureString (see ssm.tf / ecs.tf).
    # Verified live: the connected app mints a JWT that Tableau accepts for site 'jobadder'.
    TABLEAU_SERVER_URL   = "https://prod-apsoutheast-a.online.tableau.com"
    TABLEAU_SITE         = "jobadder"
    TABLEAU_CA_CLIENT_ID = "d7bd4c64-2f66-435a-83e4-719f498da78d"
    TABLEAU_CA_SECRET_ID = "83e41b22-e3e3-4dd1-8b5d-85b315507d02"
    # Dashboards shown on the Reports page, as "Label=Workbook/View" (Embedding API v3
    # path form, i.e. the REST contentUrl with "/sheets/" removed). Starter set of the
    # CS/revenue-relevant views discovered on the site; tune with the CS team.
    TABLEAU_VIEWS = "Revenue Dashboard=RevenueDashboard-Recent/RevenueDashboard, Revenue by Customer=RevenueDashboard-Recent/RevenuebyCustomer, NDR Overview Annual=NDRRevenueReporting_17663714097050/NDROverviewAnnual, NDR Summary=NDRRevenueReporting_17663714097050/NDRSummary, Billing Details=BillingDetailsDashboard/BillingDetailsDashboard, Stripe Payout=StripePayoutDashboard/StripePayOut"
    # Outbound email provider for the monthly digest (Scenario E). Option A = HubSpot
    # transactional single-send. Leave CS_EMAIL_PROVIDER empty to keep the digest in its
    # honest 'no-email-provider' state (compile + report, never send). To go live:
    #   1. Create a TRANSACTIONAL email in HubSpot (needs the transactional-email add-on)
    #      with merge tokens: account_name, period, licence_utilization_pct,
    #      active_logins_7d, top_feature_adoption_pct, tickets_resolved_30d, csat_30d,
    #      expansion_cta, recipient_name.
    #   2. Set CS_HS_TRANSACTIONAL_EMAIL_ID to that email's id.
    #   3. Set CS_EMAIL_PROVIDER = "hubspot".
    # Until (2) is set the adapter reports 'template-not-configured' and sends nothing.
    CS_EMAIL_PROVIDER            = ""
    CS_HS_TRANSACTIONAL_EMAIL_ID = ""
  }
}

variable "redshift_assume_role_arn" {
  description = "Cross-account role in the Data Platform account the task assumes for the Redshift Data API (churn). Empty => churn adapter reports not-live and the platform falls back to the computed score."
  type        = string
  default     = "arn:aws:iam::503561421603:role/cs-platform-churn-reader"
}

# ---------------------------------------------------------------------------
# Monthly digest scheduler (Scenario E auto-run)
# ---------------------------------------------------------------------------
# A 1st-of-month EventBridge Scheduler that starts a one-shot ECS task from the SAME
# task definition, overriding the command to run platform/digest_runner.py. It inherits
# the app's task role + SSM secrets, so adapters are live with no new auth surface and
# no ALB exposure. OFF by default so no scheduled run exists until an outbound email
# provider (CS_EMAIL_PROVIDER) is connected; even when on, the per-account honesty gates
# mean nothing sends until the provider is set and digest_apply is true.
variable "digest_schedule_enabled" {
  description = "Create the 1st-of-month monthly-digest scheduler. Keep false until an outbound email provider is connected; a dry-run schedule can be enabled earlier to validate the batch compile in CloudWatch."
  type        = bool
  default     = false
}

variable "digest_schedule_expression" {
  description = "EventBridge Scheduler expression for the monthly digest run (default: 08:00 on the 1st, Sydney time via schedule_timezone)."
  type        = string
  default     = "cron(0 8 1 * ? *)"
}

variable "digest_schedule_timezone" {
  description = "IANA timezone the schedule expression is evaluated in."
  type        = string
  default     = "Australia/Sydney"
}

variable "digest_apply" {
  description = "Whether the scheduled run requests apply=true (gated send). Even true is a no-op until CS_EMAIL_PROVIDER is connected and CS_ALLOW_WRITE=1. Keep false for a compile-only dry-run schedule."
  type        = bool
  default     = false
}

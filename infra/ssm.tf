# ---------------------------------------------------------------------------
# SSM SecureString parameters for CS Platform secrets.
#
# Terraform creates the parameter *slots* with placeholder values; the real
# secret values are populated out-of-band (never committed). lifecycle.ignore_changes
# on `value` means Terraform will not clobber a rotated secret on later applies.
#
# Namespace: /cs-platform/*  (standalone — not shared with ja-observe).
# ---------------------------------------------------------------------------

locals {
  secret_params = {
    "auth/session-secret"           = "AUTH_SECRET — session cookie HMAC key (openssl rand -base64 32)"
    "auth/cognito-client-secret"    = "AUTH_COGNITO_SECRET — Cognito app client secret"
    "sources/hubspot-token"         = "HUBSPOT_TOKEN — HubSpot private app token"
    "sources/stripe-key"            = "STRIPE_KEY — Stripe restricted read-only key (rk_live_...)"
    "sources/pendo-key"             = "PENDO_KEY — Pendo integration key"
    "sources/zendesk-token"         = "ZENDESK_TOKEN — Zendesk API token"
    "sources/rocket-lane-key"       = "ROCKET_LANE_KEY — Rocket Lane CS Platform API key"
    "sources/jiminny-key"           = "JIMINNY_KEY — Jiminny CS Platform API key"
    "tableau/ca-secret-value"       = "TABLEAU_CA_SECRET_VALUE — Tableau connected-app secret (JWT signing key)"
    "sources/roi-ai-webhook-secret" = "ROI_AI_WEBHOOK_SECRET — shared HMAC secret for the ROI AI telemetry webhook"
  }
}

resource "aws_ssm_parameter" "secrets" {
  for_each = local.secret_params

  name        = "/${local.name}/${each.key}"
  description = each.value
  type        = "SecureString"
  value       = "PLACEHOLDER_SET_OUT_OF_BAND"

  lifecycle {
    ignore_changes = [value]
  }

  tags = local.tags
}

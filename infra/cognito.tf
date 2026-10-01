# ---------------------------------------------------------------------------
# DEDICATED Cognito user pool for CS Platform.
#
# This REPLACES the shared ja-observe Cognito pool. CS Platform now has its own
# SSO: its own user pool, its own SAML federation to AWS Identity Center, its own
# app client and hosted UI. One sign-in here is independent of JA Observe.
#
# Federation chain (same corporate pattern, standalone instance):
#   User -> Cognito Hosted UI -> SAML -> AWS Identity Center -> Okta
#
# The SAML "Group" claim is mapped to the Cognito custom:groups attribute, which
# platform/rbac.py reads to resolve admin vs scoped-CSM. Create a DEDICATED
# Identity Center SAML app for CS Platform and point var.saml_metadata_url at it.
# ---------------------------------------------------------------------------

resource "aws_cognito_user_pool" "main" {
  name = "${local.name}-auth"

  # SSO-only pool: identities are federated, not self-registered.
  admin_create_user_config {
    allow_admin_create_user_only = true
  }

  # custom:groups carries the SAML Group claim -> consumed by rbac.resolve_role.
  schema {
    name                = "groups"
    attribute_data_type = "String"
    mutable             = true
    required            = false
    string_attribute_constraints {
      min_length = 0
      max_length = 2048
    }
  }

  username_attributes      = ["email"]
  auto_verified_attributes = ["email"]

  user_pool_add_ons {
    advanced_security_mode = "AUDIT"
  }

  tags = local.tags
}

# Hosted UI domain: https://<prefix>.auth.<region>.amazoncognito.com
resource "aws_cognito_user_pool_domain" "main" {
  domain       = var.cognito_domain_prefix
  user_pool_id = aws_cognito_user_pool.main.id
}

# ---------------------------------------------------------------------------
# SAML federation to AWS Identity Center (optional until the IdC app exists).
# ---------------------------------------------------------------------------
resource "aws_cognito_identity_provider" "saml" {
  count = var.saml_metadata_url == "" ? 0 : 1

  user_pool_id  = aws_cognito_user_pool.main.id
  provider_name = "JobAdderSSO"
  provider_type = "SAML"

  provider_details = {
    MetadataURL = var.saml_metadata_url
    IDPSignout  = "true"
  }

  # Map SAML assertion claims -> Cognito attributes. The "Group" claim becomes
  # custom:groups, which rbac.py uses to resolve the CS Platform role.
  attribute_mapping = {
    email           = "email"
    name            = "name"
    "custom:groups" = "http://schemas.xmlsoap.org/claims/Group"
  }
}

# ---------------------------------------------------------------------------
# App client for the CS Platform server (Python OIDC + PKCE, confidential client).
# Callback path is /auth/callback (NOT NextAuth's /api/auth/callback/cognito).
# ---------------------------------------------------------------------------
resource "aws_cognito_user_pool_client" "app" {
  name         = "${local.name}-client"
  user_pool_id = aws_cognito_user_pool.main.id

  generate_secret                      = true
  allowed_oauth_flows                  = ["code"]
  allowed_oauth_flows_user_pool_client = true
  allowed_oauth_scopes                 = ["openid", "profile", "email"]

  supported_identity_providers = var.saml_metadata_url == "" ? ["COGNITO"] : ["JobAdderSSO"]

  callback_urls = [local.oidc_callback]
  logout_urls   = [local.public_url, "${local.public_url}/login?logged_out=true"]

  # PKCE confidential client; prevent token leakage.
  prevent_user_existence_errors = "ENABLED"
  enable_token_revocation       = true

  access_token_validity  = 60
  id_token_validity      = 60
  refresh_token_validity = 8
  token_validity_units {
    access_token  = "minutes"
    id_token      = "minutes"
    refresh_token = "hours"
  }

  explicit_auth_flows = ["ALLOW_REFRESH_TOKEN_AUTH"]

  depends_on = [aws_cognito_identity_provider.saml]
}

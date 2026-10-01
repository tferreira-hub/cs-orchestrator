# Discover existing networking in the Tooling account. The VPC/subnets/NAT already
# exist (vpc-au-tooling, 10.200.0.0/16); CS Platform does not own or create them.

data "aws_caller_identity" "current" {}

data "aws_region" "current" {}

data "aws_vpc" "main" {
  id = var.vpc_id
}

locals {
  name           = var.name_prefix # "cs-platform"
  cognito_issuer = "https://cognito-idp.${var.region}.amazonaws.com/${aws_cognito_user_pool.main.id}"
  hosted_ui      = "https://${var.cognito_domain_prefix}.auth.${var.region}.amazoncognito.com"
  public_url     = "https://${var.domain_name}"
  oidc_callback  = "https://${var.domain_name}/auth/callback"

  tags = {
    Application = "cs-platform"
    Environment = var.environment
  }
}

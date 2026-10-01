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

  # Mandatory JobAdder tag taxonomy, applied to every resource. Optional tags
  # (JAInstance, LifecycleEndDate) are included only when set.
  tags = merge(
    {
      Owner       = var.tag_owner
      Repository  = var.tag_repository
      Application = var.tag_application
      Environment = var.environment
      IaC         = "terraform"
      Lifecycle   = var.tag_lifecycle
    },
    var.tag_ja_instance == "" ? {} : { JAInstance = var.tag_ja_instance },
    var.tag_lifecycle_end_date == "" ? {} : { LifecycleEndDate = var.tag_lifecycle_end_date },
  )
}

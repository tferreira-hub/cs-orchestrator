# CS Platform — standalone infrastructure.
#
# CS Platform is a COMPLETELY SEPARATE tool from JA Observe. This stack provisions
# its own dedicated footprint in the Tooling account (350067031910): its own Cognito
# user pool + SSO, its own ALB, ECR, ECS cluster/service, IAM roles, SSM parameters,
# CloudWatch logs, and the csplatform.jobadder.tools domain. Nothing here is shared
# with, or depends on, the ja-observe-* resources in the DevOps account.

terraform {
  required_version = ">= 1.5.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.60"
    }
  }

  # Remote state in the Tooling account's Terraform state bucket (verified to exist:
  # au.terraform-state.tooling.jobadder). Kept as a partial config so
  # `terraform init -backend=false` still works for offline validation.
  backend "s3" {
    bucket  = "au.terraform-state.tooling.jobadder"
    key     = "cs-platform/terraform.tfstate"
    region  = "ap-southeast-2"
    encrypt = true
    # Add a DynamoDB lock table if the bucket's state tooling expects one:
    # dynamodb_table = "<tooling-tfstate-locks>"
  }
}

provider "aws" {
  region = var.region

  # Guardrail: refuse to apply unless the credentials target the Tooling account.
  allowed_account_ids = [var.account_id]

  default_tags {
    tags = {
      Application = "cs-platform"
      ManagedBy   = "terraform"
      Repo        = "cs-orchestrator"
      Environment = var.environment
    }
  }
}

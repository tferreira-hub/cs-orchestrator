# ---------------------------------------------------------------------------
# Terraform infra-provisioning OIDC role (the TF_DEPLOY_ROLE_ARN_TOOLING secret).
#
# This role is the bootstrap that the `infra` pipeline assumes to run Terraform. It
# is OWNED BY CLOUDFORMATION, not Terraform, to solve the chicken-and-egg cleanly:
# Terraform cannot create the role it authenticates with. The role is defined
# declaratively in infra/bootstrap/tf-oidc-bootstrap.yaml and deployed once via
# `aws cloudformation deploy` (see infra/RUNBOOK.md). Everything else is Terraform.
#
# Here we only REFERENCE it (data source) so the stack can surface its ARN as an
# output and so there is a single, documented source of truth.
# ---------------------------------------------------------------------------

data "aws_iam_role" "tf_deploy" {
  name = "${local.name}-tf-deploy"
}

output "tf_deploy_role_arn" {
  description = "GitHub secret TF_DEPLOY_ROLE_ARN_TOOLING. Owned by the CloudFormation bootstrap stack (infra/bootstrap/)."
  value       = data.aws_iam_role.tf_deploy.arn
}

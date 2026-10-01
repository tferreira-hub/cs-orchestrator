# CS Platform — Tooling account (350067031910) deployment values.
# Networking/cert/account defaults are already set in variables.tf from the live
# Tooling VPC; override here only if they change.

account_id  = "350067031910"
region      = "ap-southeast-2"
environment = "prod"
name_prefix = "cs-platform"

domain_name           = "csplatform.jobadder.tools"
cognito_domain_prefix = "csplatform-jobadder"
admin_group_name      = "CS-Platform-Admins"

# Dedicated CS Platform SAML app in AWS Identity Center (Manager account 891377124793).
# This is CS Platform's OWN app (metadata id ...7223ce73bceb4c09), independent of the
# ja-observe app. Setting this makes Terraform create the pool's own JobAdderSSO SAML
# IdP and point the app client at it.
saml_metadata_url = "https://portal.sso.us-east-1.amazonaws.com/saml/metadata/ODkxMzc3MTI0NzkzX2lucy03MjIzY2U3M2JjZWI0YzA5"

# Group IDs (UUIDs from Identity Center) govern access + admin. Fill these once the
# CS-Platform-Admins group ID is known: both grant access, admin set grants admin.
cs_admin_group_ids = ""
cs_user_group_ids  = ""
# Break-glass for cutover: lets a named admin in BEFORE the group IDs are wired, so we
# can verify the Okta sign-in end to end. Replace with group IDs and clear this after.
auth_admin_emails = "tferreira@jobadder.com"

# First-apply image is bootstrapped from ECR; the CI pipeline swaps the real image.
# Optionally pin an initial image here, e.g.:
# container_image = "350067031910.dkr.ecr.ap-southeast-2.amazonaws.com/cs-platform:<sha>"

# IMPORTANT: start at 0 so the FIRST `terraform apply` can create the service before
# any image exists in ECR (the repo is IMMUTABLE and the bootstrap tag won't exist
# yet). After the CI pipeline pushes the first image, scale up — the service's
# ignore_changes=[desired_count] means Terraform won't revert the pipeline/you.
#   aws ecs update-service --cluster cs-platform --service cs-platform --desired-count 1
desired_count = 0
task_cpu      = 512
task_memory   = 1024

# Cross-account Redshift churn reader (Data Platform account). The adapter assumes
# this; leave as-is to enable live ML churn, or set "" to fall back to computed risk.
redshift_assume_role_arn = "arn:aws:iam::503561421603:role/cs-platform-churn-reader"

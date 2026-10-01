output "alb_dns_name" {
  description = "ALB hostname. Point the Cloudflare CNAME for csplatform.jobadder.tools at this."
  value       = aws_lb.main.dns_name
}

output "ecr_repository_url" {
  description = "ECR repo the CI pipeline pushes images to."
  value       = aws_ecr_repository.app.repository_url
}

output "ecs_cluster_name" {
  description = "ECS cluster name (for the deploy workflow)."
  value       = aws_ecs_cluster.main.name
}

output "ecs_service_name" {
  description = "ECS service name (for the deploy workflow)."
  value       = aws_ecs_service.app.name
}

output "cognito_user_pool_id" {
  description = "Dedicated CS Platform Cognito user pool id."
  value       = aws_cognito_user_pool.main.id
}

output "cognito_client_id" {
  description = "CS Platform app client id (AUTH_COGNITO_ID)."
  value       = aws_cognito_user_pool_client.app.id
}

output "cognito_issuer" {
  description = "OIDC issuer (AUTH_COGNITO_ISSUER)."
  value       = local.cognito_issuer
}

output "cognito_hosted_ui" {
  description = "Cognito hosted UI base URL."
  value       = local.hosted_ui
}

output "cognito_client_secret_ssm" {
  description = "SSM path to populate with the Cognito app client secret."
  value       = aws_ssm_parameter.secrets["auth/cognito-client-secret"].name
}

output "task_role_arn" {
  description = "Task role ARN — add this as the trusted principal on the Data Platform churn reader role."
  value       = aws_iam_role.task.arn
}

output "public_url" {
  description = "Public URL once the Cloudflare DNS record resolves."
  value       = local.public_url
}

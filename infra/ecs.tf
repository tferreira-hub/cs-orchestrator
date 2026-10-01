# ---------------------------------------------------------------------------
# ECS Fargate cluster, task definition, and service for CS Platform.
# Standalone cluster "cs-platform" (not ja-observe-devops).
# ---------------------------------------------------------------------------

resource "aws_cloudwatch_log_group" "app" {
  name              = "/ecs/${local.name}"
  retention_in_days = 30
  tags              = local.tags
}

resource "aws_ecs_cluster" "main" {
  name = local.name

  setting {
    name  = "containerInsights"
    value = "enabled"
  }

  tags = local.tags
}

resource "aws_ecs_cluster_capacity_providers" "main" {
  cluster_name       = aws_ecs_cluster.main.name
  capacity_providers = ["FARGATE"]

  default_capacity_provider_strategy {
    capacity_provider = "FARGATE"
    weight            = 1
  }
}

locals {
  # Image Terraform registers on first apply. If var.container_image is empty we
  # bootstrap with the ECR repo URL + "bootstrap" tag; the CI pipeline then swaps
  # in the real git-sha image on subsequent deploys (service ignores task def).
  image = var.container_image != "" ? var.container_image : "${aws_ecr_repository.app.repository_url}:bootstrap"

  # Non-secret env, merged with the values Terraform owns (Cognito/public URL/role).
  computed_env = merge(var.app_environment, {
    CS_PUBLIC_URL            = local.public_url
    AUTH_COGNITO_ISSUER      = local.cognito_issuer
    AUTH_COGNITO_ID          = aws_cognito_user_pool_client.app.id
    AUTH_COGNITO_DOMAIN      = var.cognito_domain_prefix
    CS_ADMIN_GROUPS          = var.cs_admin_group_ids
    CS_USER_GROUPS           = var.cs_user_group_ids
    AUTH_ADMIN_EMAILS        = var.auth_admin_emails
    REDSHIFT_ASSUME_ROLE_ARN = var.redshift_assume_role_arn
  })

  container_environment = [for k, v in local.computed_env : { name = k, value = tostring(v) }]

  container_secrets = [
    { name = "AUTH_SECRET", valueFrom = aws_ssm_parameter.secrets["auth/session-secret"].arn },
    { name = "AUTH_COGNITO_SECRET", valueFrom = aws_ssm_parameter.secrets["auth/cognito-client-secret"].arn },
    { name = "HUBSPOT_TOKEN", valueFrom = aws_ssm_parameter.secrets["sources/hubspot-token"].arn },
    { name = "STRIPE_KEY", valueFrom = aws_ssm_parameter.secrets["sources/stripe-key"].arn },
    { name = "PENDO_KEY", valueFrom = aws_ssm_parameter.secrets["sources/pendo-key"].arn },
    { name = "ZENDESK_TOKEN", valueFrom = aws_ssm_parameter.secrets["sources/zendesk-token"].arn },
    { name = "ROCKET_LANE_KEY", valueFrom = aws_ssm_parameter.secrets["sources/rocket-lane-key"].arn },
    { name = "JIMINNY_KEY", valueFrom = aws_ssm_parameter.secrets["sources/jiminny-key"].arn },
  ]
}

resource "aws_ecs_task_definition" "app" {
  family                   = local.name
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.task_cpu
  memory                   = var.task_memory
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn

  container_definitions = jsonencode([
    {
      name            = local.name
      image           = local.image
      essential       = true
      user            = "1000:1000"
      portMappings    = [{ containerPort = var.container_port, protocol = "tcp" }]
      environment     = local.container_environment
      secrets         = local.container_secrets
      linuxParameters = { initProcessEnabled = true }
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.app.name
          "awslogs-region"        = var.region
          "awslogs-stream-prefix" = local.name
        }
      }
      healthCheck = {
        command     = ["CMD-SHELL", "python3 -c \"import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:${var.container_port}/login',timeout=3).status in (200,302) else 1)\" || exit 1"]
        interval    = 30
        timeout     = 5
        retries     = 3
        startPeriod = 30
      }
    }
  ])

  tags = local.tags
}

resource "aws_ecs_service" "app" {
  name            = local.name
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.app.arn
  desired_count   = var.desired_count
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = var.private_subnet_ids
    security_groups  = [aws_security_group.service.id]
    assign_public_ip = false
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.app.arn
    container_name   = local.name
    container_port   = var.container_port
  }

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  deployment_minimum_healthy_percent = 100
  deployment_maximum_percent         = 200

  # Terraform owns the task-def SHAPE (env/secrets/roles). The CI pipeline swaps
  # the running IMAGE and registers new revisions, so ignore task_definition and
  # desired_count drift from the pipeline/autoscaling.
  lifecycle {
    ignore_changes = [task_definition, desired_count]
  }

  depends_on = [aws_lb_listener.https]

  tags = local.tags
}

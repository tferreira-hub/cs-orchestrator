# ---------------------------------------------------------------------------
# Monthly account performance digest scheduler (Scenario E auto-run).
#
# EventBridge Scheduler fires on the 1st of each month and starts a ONE-SHOT ECS
# Fargate task from the SAME task definition family as the web service, overriding
# the container command to run platform/digest_runner.py. Because it reuses the task
# role and SSM secrets, the vendor adapters are live exactly as they are for the app,
# with no new auth surface and no ALB exposure.
#
# Gated/safe by construction:
#   * The whole scheduler is created only when var.digest_schedule_enabled = true.
#   * digest_runner runs as the SYSTEM principal (whole book), but every per-account
#     honesty gate inside engine.run_monthly_digests is preserved: writes-off,
#     no recipient, and no email provider all result in compile-and-report, never a send.
#   * var.digest_apply controls whether apply=true is requested; even true is a no-op
#     until CS_EMAIL_PROVIDER is connected. Leave it false for a compile-only dry-run.
# ---------------------------------------------------------------------------

locals {
  digest_count = var.digest_schedule_enabled ? 1 : 0
}

# --- Scheduler execution role: RunTask + PassRole the task/execution roles ----
data "aws_iam_policy_document" "scheduler_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["scheduler.amazonaws.com"]
    }
    # Confused-deputy guard: only this account's scheduler may assume the role.
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [var.account_id]
    }
  }
}

resource "aws_iam_role" "digest_scheduler" {
  count              = local.digest_count
  name               = "${local.name}-digest-scheduler"
  assume_role_policy = data.aws_iam_policy_document.scheduler_assume.json
  tags               = local.tags
}

data "aws_iam_policy_document" "digest_scheduler" {
  # Run the digest task on the cluster. Scoped to this task-def family's revisions.
  statement {
    sid       = "RunDigestTask"
    actions   = ["ecs:RunTask"]
    resources = ["${replace(aws_ecs_task_definition.app.arn, "/:\\d+$/", "")}:*"]
    condition {
      test     = "ArnEquals"
      variable = "ecs:cluster"
      values   = [aws_ecs_cluster.main.arn]
    }
  }
  # PassRole the task + execution roles to ECS (required for RunTask).
  statement {
    sid       = "PassTaskRoles"
    actions   = ["iam:PassRole"]
    resources = [aws_iam_role.task.arn, aws_iam_role.execution.arn]
    condition {
      test     = "StringEquals"
      variable = "iam:PassedToService"
      values   = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role_policy" "digest_scheduler" {
  count  = local.digest_count
  name   = "${local.name}-digest-scheduler"
  role   = aws_iam_role.digest_scheduler[0].id
  policy = data.aws_iam_policy_document.digest_scheduler.json
}

# --- The schedule -----------------------------------------------------------
resource "aws_scheduler_schedule" "monthly_digest" {
  count                        = local.digest_count
  name                         = "${local.name}-monthly-digest"
  group_name                   = "default"
  schedule_expression          = var.digest_schedule_expression
  schedule_expression_timezone = var.digest_schedule_timezone
  state                        = "ENABLED"

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = aws_ecs_cluster.main.arn
    role_arn = aws_iam_role.digest_scheduler[0].arn

    ecs_parameters {
      # Family ARN (no revision) so the schedule always runs the latest registered
      # task definition the CI pipeline deployed.
      task_definition_arn = replace(aws_ecs_task_definition.app.arn, "/:\\d+$/", "")
      task_count          = 1
      launch_type         = "FARGATE"

      network_configuration {
        subnets          = var.private_subnet_ids
        security_groups  = [aws_security_group.service.id]
        assign_public_ip = false
      }
    }

    retry_policy {
      maximum_retry_attempts = 2
    }

    # Override just the container command to the one-shot runner. apply is passed via
    # the env override so the schedule, not the image, controls the gate.
    input = jsonencode({
      containerOverrides = [
        {
          name    = local.name
          command = ["python3", "platform/digest_runner.py"]
          environment = [
            { name = "CS_DIGEST_APPLY", value = var.digest_apply ? "1" : "0" }
          ]
        }
      ]
    })
  }
}

output "digest_schedule_name" {
  description = "Monthly-digest schedule name (empty when disabled)."
  value       = local.digest_count == 1 ? aws_scheduler_schedule.monthly_digest[0].name : ""
}

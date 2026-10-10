# ---------------------------------------------------------------------------
# IAM roles for the ECS task.
#   * execution role: pull from ECR, write logs, read the SSM secrets at launch.
#   * task role: runtime permissions — cross-account STS (Redshift churn) + Bedrock.
# Both are least-privilege and scoped to this app's resources.
# ---------------------------------------------------------------------------

data "aws_iam_policy_document" "ecs_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

# --- Execution role ---------------------------------------------------------
resource "aws_iam_role" "execution" {
  name               = "${local.name}-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
  tags               = local.tags
}

resource "aws_iam_role_policy_attachment" "execution_managed" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

# Allow the execution role to read ONLY this app's SSM secrets at container launch.
data "aws_iam_policy_document" "execution_ssm" {
  statement {
    sid       = "ReadCsPlatformSecrets"
    actions   = ["ssm:GetParameters", "ssm:GetParameter"]
    resources = ["arn:aws:ssm:${var.region}:${var.account_id}:parameter/${local.name}/*"]
  }
}

resource "aws_iam_role_policy" "execution_ssm" {
  name   = "${local.name}-execution-ssm"
  role   = aws_iam_role.execution.id
  policy = data.aws_iam_policy_document.execution_ssm.json
}

# --- Task role --------------------------------------------------------------
resource "aws_iam_role" "task" {
  name               = "${local.name}-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
  tags               = local.tags
}

data "aws_iam_policy_document" "task" {
  # Cross-account assume into the Data Platform Redshift churn reader (if configured).
  dynamic "statement" {
    for_each = var.redshift_assume_role_arn == "" ? [] : [var.redshift_assume_role_arn]
    content {
      sid       = "AssumeChurnReader"
      actions   = ["sts:AssumeRole"]
      resources = [statement.value]
    }
  }

  # Bedrock Converse for the agent runner (region-scoped foundation/inference models).
  statement {
    sid     = "BedrockInvoke"
    actions = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"]
    resources = [
      "arn:aws:bedrock:*::foundation-model/*",
      "arn:aws:bedrock:*:${var.account_id}:inference-profile/*",
    ]
  }

  # Read its own SSM secrets at runtime (adapters re-read on demand).
  statement {
    sid       = "ReadOwnSecrets"
    actions   = ["ssm:GetParameter", "ssm:GetParameters"]
    resources = ["arn:aws:ssm:${var.region}:${var.account_id}:parameter/${local.name}/*"]
  }

  # Mount + read/write the EFS data volume (persistent append-only state), scoped to
  # this file system and only via its access point (transit encryption enforced).
  statement {
    sid = "EfsDataAccess"
    actions = [
      "elasticfilesystem:ClientMount",
      "elasticfilesystem:ClientWrite",
      "elasticfilesystem:ClientRootAccess",
    ]
    resources = [aws_efs_file_system.data.arn]
    condition {
      test     = "StringEquals"
      variable = "elasticfilesystem:AccessPointArn"
      values   = [aws_efs_access_point.data.arn]
    }
  }
}

resource "aws_iam_role_policy" "task" {
  name   = "${local.name}-task"
  role   = aws_iam_role.task.id
  policy = data.aws_iam_policy_document.task.json
}

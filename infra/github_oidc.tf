# ---------------------------------------------------------------------------
# GitHub Actions OIDC deploy role (the ECR_PUSH_ROLE_ARN_TOOLING repo secret).
#
# The build-and-deploy workflow assumes this role via GitHub OIDC (no long-lived
# keys). It is scoped to the main branch of JobAdder/cs-orchestrator and granted
# exactly what the pipeline needs: push to the cs-platform ECR repo and register
# a new ECS task-def revision + update the service.
#
# The OIDC provider already exists in the Tooling account
# (arn:aws:iam::350067031910:oidc-provider/token.actions.githubusercontent.com,
# audience sts.amazonaws.com) — referenced as a data source, not re-created.
# ---------------------------------------------------------------------------

variable "github_repo" {
  description = "GitHub org/repo allowed to assume the deploy role."
  type        = string
  default     = "JobAdder/cs-orchestrator"
}

variable "github_deploy_ref" {
  description = "Git ref (branch) allowed to deploy. Scopes the OIDC subject claim."
  type        = string
  default     = "refs/heads/main"
}

data "aws_iam_openid_connect_provider" "github" {
  url = "https://token.actions.githubusercontent.com"
}

data "aws_iam_policy_document" "github_assume" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    effect  = "Allow"

    principals {
      type        = "Federated"
      identifiers = [data.aws_iam_openid_connect_provider.github.arn]
    }

    # Audience must be sts.amazonaws.com (the provider's registered client id).
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }

    # Only this repo + branch may assume the role. JobAdder's GitHub OIDC tokens use
    # the immutable-ID subject format (repo:ORG@<orgid>/REPO@<repoid>:...), so we match
    # both the plain and immutable-ID forms. Confirmed via CloudTrail + the working
    # ja-observe-devops-github-actions role.
    condition {
      test     = "StringLike"
      variable = "token.actions.githubusercontent.com:sub"
      values = [
        "repo:${var.github_repo}:ref:${var.github_deploy_ref}",
        "repo:JobAdder@*/cs-orchestrator@*:ref:${var.github_deploy_ref}",
      ]
    }
  }
}

resource "aws_iam_role" "github_deploy" {
  name                 = "${local.name}-github-deploy"
  assume_role_policy   = data.aws_iam_policy_document.github_assume.json
  max_session_duration = 3600
  tags                 = local.tags
}

data "aws_iam_policy_document" "github_deploy" {
  # ECR: auth + push/pull to the cs-platform repo only.
  statement {
    sid       = "EcrAuth"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"] # GetAuthorizationToken does not support resource scoping
  }

  statement {
    sid = "EcrPushPull"
    actions = [
      "ecr:BatchCheckLayerAvailability",
      "ecr:GetDownloadUrlForLayer",
      "ecr:BatchGetImage",
      "ecr:InitiateLayerUpload",
      "ecr:UploadLayerPart",
      "ecr:CompleteLayerUpload",
      "ecr:PutImage",
    ]
    resources = [aws_ecr_repository.app.arn]
  }

  # ECS: register a new task-def revision and update the service.
  statement {
    sid = "EcsDeploy"
    actions = [
      "ecs:RegisterTaskDefinition",
      "ecs:DescribeTaskDefinition",
      "ecs:DescribeServices",
      "ecs:UpdateService",
      "ecs:TagResource",
    ]
    resources = ["*"] # RegisterTaskDefinition/DescribeTaskDefinition are not resource-scopable
  }

  # The pipeline must pass the task + execution roles into the task definition.
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

resource "aws_iam_role_policy" "github_deploy" {
  name   = "${local.name}-github-deploy"
  role   = aws_iam_role.github_deploy.id
  policy = data.aws_iam_policy_document.github_deploy.json
}

output "github_deploy_role_arn" {
  description = "Set this as the GitHub repo secret ECR_PUSH_ROLE_ARN_TOOLING."
  value       = aws_iam_role.github_deploy.arn
}

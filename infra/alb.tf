# ---------------------------------------------------------------------------
# Dedicated internet-facing ALB for csplatform.jobadder.tools.
# Cloudflare proxies the public hostname to this ALB (DNS record added in
# Cloudflare, not Terraform — see RUNBOOK). TLS terminates here using the
# existing *.jobadder.tools ACM cert.
# ---------------------------------------------------------------------------

resource "aws_security_group" "alb" {
  name        = "${local.name}-alb"
  description = "CS Platform ALB ingress (HTTPS/HTTP from Cloudflare edge only)"
  vpc_id      = var.vpc_id

  ingress {
    description = "HTTPS from Cloudflare edge"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = var.cloudflare_ipv4_cidrs
  }

  ingress {
    description = "HTTP from Cloudflare edge (redirected to HTTPS)"
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = var.cloudflare_ipv4_cidrs
  }

  tags = merge(local.tags, { Name = "${local.name}-alb" })
}

resource "aws_security_group" "service" {
  name        = "${local.name}-service"
  description = "CS Platform Fargate tasks (ingress only from the ALB)"
  vpc_id      = var.vpc_id

  # Egress is HTTPS-only. The task must reach arbitrary vendor API hosts (HubSpot,
  # Zendesk, Stripe, Pendo, Jiminny) plus AWS endpoints (ECR, SSM, STS, Bedrock)
  # over the NAT gateway — these are not a fixed IP set — but all over TCP 443.
  egress {
    description = "HTTPS to vendor APIs and AWS endpoints"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge(local.tags, { Name = "${local.name}-service" })
}

# Cross-referencing rules are standalone to avoid a circular SG dependency
# (ALB egress -> service, service ingress <- ALB).
resource "aws_security_group_rule" "alb_egress_to_service" {
  type                     = "egress"
  description              = "ALB to ECS tasks on the app port"
  from_port                = var.container_port
  to_port                  = var.container_port
  protocol                 = "tcp"
  security_group_id        = aws_security_group.alb.id
  source_security_group_id = aws_security_group.service.id
}

resource "aws_security_group_rule" "service_ingress_from_alb" {
  type                     = "ingress"
  description              = "App port from ALB only"
  from_port                = var.container_port
  to_port                  = var.container_port
  protocol                 = "tcp"
  security_group_id        = aws_security_group.service.id
  source_security_group_id = aws_security_group.alb.id
}

resource "aws_lb" "main" {
  name               = "${local.name}-alb"
  internal           = false
  load_balancer_type = "application"
  security_groups    = [aws_security_group.alb.id]
  subnets            = var.public_subnet_ids
  ip_address_type    = "ipv4" # Tooling public subnets have no IPv6 CIDR; Cloudflare reaches an IPv4 ALB fine

  drop_invalid_header_fields = true

  tags = merge(local.tags, { Name = "${local.name}-alb" })
}

resource "aws_lb_target_group" "app" {
  name        = "${local.name}-tg"
  port        = var.container_port
  protocol    = "HTTP"
  vpc_id      = var.vpc_id
  target_type = "ip"

  health_check {
    path                = "/login"
    protocol            = "HTTP"
    matcher             = "200,302"
    interval            = 30
    timeout             = 5
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }

  deregistration_delay = 30

  tags = merge(local.tags, { Name = "${local.name}-tg" })
}

resource "aws_lb_listener" "https" {
  load_balancer_arn = aws_lb.main.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = var.acm_certificate_arn

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.app.arn
  }
}

resource "aws_lb_listener" "http_redirect" {
  load_balancer_arn = aws_lb.main.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type = "redirect"
    redirect {
      port        = "443"
      protocol    = "HTTPS"
      status_code = "HTTP_301"
    }
  }
}

# ---------------------------------------------------------------------------
# Persistent storage for the CS Platform's append-only JSONL state.
#
# WHY: health-history snapshots, the F2F log, ROI AI telemetry, task events, and
# the audit trail are written as local JSONL files. On Fargate the container
# filesystem is EPHEMERAL, so every deploy / task restart wiped them. That is why
# health TREND risks never fired ("No declining trend over the last 45 days"): the
# history was reset on every deploy and could never accumulate the >=2 daily
# snapshots over a 45-day window the trend engine needs.
#
# This mounts an EFS volume at /data and points CS_HISTORY_FILE (and the other
# JSONL paths) at it, so the state survives restarts and accumulates over time.
# EFS (not EBS) because Fargate tasks need a shared, multi-AZ, serverless volume.
# ---------------------------------------------------------------------------

resource "aws_efs_file_system" "data" {
  creation_token = "${local.name}-data"
  encrypted      = true

  lifecycle_policy {
    transition_to_ia = "AFTER_30_DAYS"
  }

  tags = merge(local.tags, { Name = "${local.name}-data" })
}

# NFS access from the Fargate task security group only.
resource "aws_security_group" "efs" {
  name        = "${local.name}-efs"
  description = "CS Platform EFS (NFS 2049 from the Fargate service only)"
  vpc_id      = var.vpc_id

  tags = merge(local.tags, { Name = "${local.name}-efs" })
}

resource "aws_security_group_rule" "efs_ingress_from_service" {
  type                     = "ingress"
  description              = "NFS from the CS Platform tasks"
  from_port                = 2049
  to_port                  = 2049
  protocol                 = "tcp"
  security_group_id        = aws_security_group.efs.id
  source_security_group_id = aws_security_group.service.id
}

# The service egress is otherwise HTTPS-only; allow NFS out to the EFS SG so the
# task can mount the volume.
resource "aws_security_group_rule" "service_egress_to_efs" {
  type                     = "egress"
  description              = "NFS to the CS Platform EFS"
  from_port                = 2049
  to_port                  = 2049
  protocol                 = "tcp"
  security_group_id        = aws_security_group.service.id
  source_security_group_id = aws_security_group.efs.id
}

# One mount target per private subnet (multi-AZ availability).
resource "aws_efs_mount_target" "data" {
  for_each        = toset(var.private_subnet_ids)
  file_system_id  = aws_efs_file_system.data.id
  subnet_id       = each.value
  security_groups = [aws_security_group.efs.id]
}

# POSIX access point so the non-root container user (1000:1000) owns /data.
resource "aws_efs_access_point" "data" {
  file_system_id = aws_efs_file_system.data.id

  posix_user {
    uid = 1000
    gid = 1000
  }

  root_directory {
    path = "/cs-platform"
    creation_info {
      owner_uid   = 1000
      owner_gid   = 1000
      permissions = "0755"
    }
  }

  tags = merge(local.tags, { Name = "${local.name}-data-ap" })
}

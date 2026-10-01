# ---------------------------------------------------------------------------
# Public DNS for csplatform.jobadder.tools.
#
# jobadder.tools is a Route 53 public zone IN THIS (Tooling) account, fronted by
# Cloudflare in PARTIAL (CNAME) setup. In partial mode the authoritative record
# stays in Route 53 and points the hostname at Cloudflare's partial-zone suffix
# (<host>.cdn.cloudflare.net); Cloudflare then proxies to the ALB. This matches the
# existing dm/singularity hosts in the same zone.
#
# So: Cloudflare proxy config is manual (done in the dashboard), the authoritative
# Route 53 record is code (here). Route 53 -> Cloudflare edge -> ALB. The ALB SG
# stays locked to Cloudflare IP ranges because traffic genuinely transits Cloudflare.
# ---------------------------------------------------------------------------

variable "route53_zone_id" {
  description = "Route 53 public hosted zone id for jobadder.tools (Tooling account)."
  type        = string
  default     = "Z0618813QQJF3P1SS6OD"
}

variable "cloudflare_partial_suffix" {
  description = "Cloudflare partial-zone (CNAME setup) suffix."
  type        = string
  default     = "cdn.cloudflare.net"
}

resource "aws_route53_record" "app" {
  zone_id = var.route53_zone_id
  name    = var.domain_name # csplatform.jobadder.tools
  type    = "CNAME"
  ttl     = 300
  records = ["${var.domain_name}.${var.cloudflare_partial_suffix}"]
}

output "dns_record" {
  description = "The public hostname and where it points (Cloudflare partial-zone edge)."
  value       = "${aws_route53_record.app.name} -> ${tolist(aws_route53_record.app.records)[0]}"
}

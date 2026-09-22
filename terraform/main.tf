# Mail sending for the watcher: a domain SES can sign for, and an IAM user that
# may send from exactly one address and do nothing else.
#
# The access key is deliberately NOT created here, so no live credential ends up
# in Terraform state. Create it once with the CLI and put it somewhere safe:
#
#   aws iam create-access-key --user-name $(terraform output -raw mailer_user)
#
# SMTP needs no SDK on the watcher host: derive the SMTP password from that
# key's secret (see the README) and point the watcher at
# email-smtp.<region>.amazonaws.com:587.

terraform {
  required_version = ">= 1.9"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.0, < 6.0"
    }
  }
}

provider "aws" {
  region = var.region

  # Everything here is tagged so it can be found and destroyed on its own.
  default_tags {
    tags = {
      project    = var.project
      managed_by = "terraform"
    }
  }
}

locals {
  sender_address = "${var.sender_local_part}@${var.mail_domain}"
}

resource "aws_sesv2_email_identity" "mail_domain" {
  email_identity = var.mail_domain

  dkim_signing_attributes {
    next_signing_key_length = "RSA_2048_BIT"
  }
}

# Easy DKIM: three CNAMEs that let SES sign as this domain. Unsigned mail from a
# new sender is treated as spam by most providers, which defeats the point of an
# alert you are waiting for.
resource "aws_route53_record" "dkim" {
  count   = 3
  zone_id = var.hosted_zone_id
  name    = "${aws_sesv2_email_identity.mail_domain.dkim_signing_attributes[0].tokens[count.index]}._domainkey.${var.mail_domain}"
  type    = "CNAME"
  ttl     = 600
  records = ["${aws_sesv2_email_identity.mail_domain.dkim_signing_attributes[0].tokens[count.index]}.dkim.amazonses.com"]
}

resource "aws_route53_record" "spf" {
  zone_id = var.hosted_zone_id
  name    = var.mail_domain
  type    = "TXT"
  ttl     = 600
  records = ["v=spf1 include:amazonses.com ~all"]
}

resource "aws_route53_record" "dmarc" {
  zone_id = var.hosted_zone_id
  name    = "_dmarc.${var.mail_domain}"
  type    = "TXT"
  ttl     = 600
  records = ["v=DMARC1; p=none; rua=mailto:${var.dmarc_report_address}"]
}

resource "aws_iam_user" "watcher" {
  name = "${var.project}-mailer"
}

resource "aws_iam_user_policy" "send_only" {
  name = "send-${var.project}"
  user = aws_iam_user.watcher.name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["ses:SendEmail", "ses:SendRawEmail"]
      Resource = "*"
      Condition = {
        StringEquals = {
          "ses:FromAddress" = local.sender_address
        }
      }
    }]
  })
}

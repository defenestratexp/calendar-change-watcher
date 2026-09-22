variable "region" {
  description = "SES region. Sending to arbitrary recipients needs production access in this region."
  type        = string
  default     = "us-west-2"
}

variable "project" {
  description = "Name used for the IAM user and the tags"
  type        = string
  default     = "calendar-watcher"
}

variable "mail_domain" {
  description = "Domain the alerts are sent from. Route 53 must host it."
  type        = string
}

variable "hosted_zone_id" {
  description = "Route 53 hosted zone id for mail_domain"
  type        = string
}

variable "sender_local_part" {
  description = "Local part of the From address"
  type        = string
  default     = "alerts"
}

variable "dmarc_report_address" {
  description = "Where DMARC aggregate reports are sent"
  type        = string
}

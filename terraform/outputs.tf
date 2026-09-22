output "sender_address" {
  description = "From address the watcher must use; the IAM policy allows no other"
  value       = local.sender_address
}

output "mailer_user" {
  description = "IAM user to create an access key for"
  value       = aws_iam_user.watcher.name
}

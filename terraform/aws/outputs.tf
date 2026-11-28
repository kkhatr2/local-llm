output "cluster_name" {
  description = "EKS cluster name."
  value       = module.eks.cluster_name
}

output "cluster_endpoint" {
  description = "Private EKS API endpoint."
  value       = module.eks.cluster_endpoint
}

output "weights_bucket" {
  description = "S3 bucket for GGUF weights."
  value       = aws_s3_bucket.weights.bucket
}

output "image_digest" {
  description = "Promoted digest deployed by Helm."
  value       = var.image_flavor == "cuda" ? var.cuda_image_digest : var.cpu_image_digest
}

output "public_hostname" {
  description = "Gateway hostname, when enabled."
  value       = var.gateway_hostname
}

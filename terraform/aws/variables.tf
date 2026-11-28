variable "region" {
  description = "AWS region for the EKS reference deployment."
  type        = string
  default     = "us-east-1"
}

variable "environment" {
  description = "Environment label applied to all resources."
  type        = string
  default     = "phase2"
}

variable "cluster_name" {
  description = "EKS cluster name."
  type        = string
  default     = "local-llm-phase2"
}

variable "vpc_cidr" {
  description = "CIDR block for the reference VPC."
  type        = string
  default     = "10.20.0.0/16"
}

variable "availability_zones" {
  description = "Exactly two AZs for the reference VPC."
  type        = list(string)
  default     = ["us-east-1a", "us-east-1b"]
}

variable "cpu_instance_type" {
  description = "Instance type for the CPU node group."
  type        = string
  default     = "m7i.large"
}

variable "gpu_instance_type" {
  description = "Instance type for the CUDA node group."
  type        = string
  default     = "g5.xlarge"
}

variable "cpu_desired_size" {
  type    = number
  default = 1
}

variable "gpu_desired_size" {
  type    = number
  default = 0
}

variable "weights_bucket_name" {
  description = "S3 bucket used by the optional object-storage model sync."
  type        = string
  default     = "local-llm-phase2-weights-change-me"
}

variable "namespace" {
  description = "Kubernetes namespace for the application."
  type        = string
  default     = "local-llm"
}

variable "gateway_hostname" {
  description = "Public hostname served by the Gateway."
  type        = string
  default     = "local-llm.example.com"
}

variable "gateway_namespace" {
  description = "Namespace allowed by the application NetworkPolicy."
  type        = string
  default     = "local-llm-gateway"
}

variable "gateway_class_name" {
  description = "Gateway API implementation class."
  type        = string
  default     = "gateway.networking.k8s.io/ingress"
}

variable "tls_secret_name" {
  description = "Kubernetes Secret containing the public TLS certificate."
  type        = string
  default     = "local-llm-tls"
}

variable "api_key_secret_name" {
  description = "Kubernetes Secret containing the gateway API key."
  type        = string
  default     = "local-llm-api-key"
}

variable "hf_token_secret_name" {
  description = "Kubernetes Secret containing an optional HF_TOKEN."
  type        = string
  default     = "local-llm-hf"
}

variable "object_credentials_secret_name" {
  description = "Kubernetes Secret containing object-storage credentials."
  type        = string
  default     = "model-supply-credentials"
}

variable "cpu_image_digest" {
  description = "Digest-only CPU image reference from deploy/image-digests.yaml."
  type        = string
  default     = "repository/local-llm-api@sha256:1111111111111111111111111111111111111111111111111111111111111111"
}

variable "cuda_image_digest" {
  description = "Digest-only CUDA image reference from deploy/image-digests.yaml."
  type        = string
  default     = "repository/local-llm-api-cuda@sha256:2222222222222222222222222222222222222222222222222222222222222222"
}

variable "image_flavor" {
  description = "Promoted image flavor: cpu or cuda."
  type        = string
  default     = "cpu"

  validation {
    condition     = contains(["cpu", "cuda"], var.image_flavor)
    error_message = "image_flavor must be cpu or cuda."
  }
}

variable "chart_path" {
  description = "Relative path to the committed Helm chart."
  type        = string
  default     = "../../helm/local-llm"
}

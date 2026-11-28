data "aws_availability_zones" "available" {
  state = "available"
}

data "aws_caller_identity" "current" {}

data "aws_eks_cluster" "cluster" {
  name       = module.eks.cluster_name
  depends_on = [module.eks]
}

data "aws_eks_cluster_auth" "cluster" {
  name       = module.eks.cluster_name
  depends_on = [module.eks]
}

provider "kubernetes" {
  host                   = data.aws_eks_cluster.cluster.endpoint
  cluster_ca_certificate = base64decode(data.aws_eks_cluster.cluster.certificate_authority[0].data)
  token                  = data.aws_eks_cluster_auth.cluster.token
}

provider "helm" {
  kubernetes {
    host                   = data.aws_eks_cluster.cluster.endpoint
    cluster_ca_certificate = base64decode(data.aws_eks_cluster.cluster.certificate_authority[0].data)
    token                  = data.aws_eks_cluster_auth.cluster.token
  }
}

module "vpc" {
  source  = "terraform-aws-modules/vpc/aws"
  version = "5.13.0"

  name = "${var.cluster_name}-vpc"
  cidr = var.vpc_cidr
  azs  = var.availability_zones

  public_subnets  = [for i in range(2) : cidrsubnet(var.vpc_cidr, 8, i)]
  private_subnets = [for i in range(2) : cidrsubnet(var.vpc_cidr, 8, i + 10)]

  enable_nat_gateway   = true
  single_nat_gateway   = true
  enable_dns_hostnames = true
  enable_dns_support   = true

  public_subnet_tags = {
    "kubernetes.io/role/elb" = "1"
  }
  private_subnet_tags = {
    "kubernetes.io/role/internal-elb" = "1"
  }
}

module "eks" {
  source  = "terraform-aws-modules/eks/aws"
  version = "20.24.0"

  cluster_name    = var.cluster_name
  cluster_version = "1.30"
  vpc_id          = module.vpc.vpc_id
  subnet_ids      = module.vpc.private_subnets

  cluster_endpoint_public_access = false
  enable_irsa                    = true

  eks_managed_node_groups = {
    cpu = {
      instance_types = [var.cpu_instance_type]
      min_size       = var.cpu_desired_size
      max_size       = var.cpu_desired_size
      desired_size   = var.cpu_desired_size
      labels = {
        "local-llm.io/compute" = "cpu"
      }
    }
    cuda = {
      instance_types = [var.gpu_instance_type]
      min_size       = 0
      max_size       = max(1, var.gpu_desired_size)
      desired_size   = var.gpu_desired_size
      labels = {
        "local-llm.io/compute" = "cuda"
      }
      taints = [
        {
          key    = "nvidia.com/gpu"
          value  = "present"
          effect = "NO_SCHEDULE"
        }
      ]
    }
  }
}

resource "aws_s3_bucket" "weights" {
  bucket = var.weights_bucket_name
}

resource "aws_s3_bucket_public_access_block" "weights" {
  bucket = aws_s3_bucket.weights.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "weights" {
  bucket = aws_s3_bucket.weights.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "weights" {
  bucket = aws_s3_bucket.weights.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_iam_policy" "object_read" {
  name = "${var.cluster_name}-object-read"
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:ListBucket"]
        Resource = [aws_s3_bucket.weights.arn, "${aws_s3_bucket.weights.arn}/*"]
      }
    ]
  })
}

data "aws_iam_policy_document" "object_read_trust" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [module.eks.oidc_provider_arn]
    }
    condition {
      test     = "StringEquals"
      variable = "${module.eks.oidc_provider}:sub"
      values   = ["system:serviceaccount:${var.namespace}:local-llm"]
    }
  }

  depends_on = [module.eks]
}

resource "aws_iam_role" "object_read" {
  name               = "${var.cluster_name}-object-read"
  assume_role_policy = data.aws_iam_policy_document.object_read_trust.json
}

resource "aws_iam_role_policy_attachment" "object_read" {
  role       = aws_iam_role.object_read.name
  policy_arn = aws_iam_policy.object_read.arn
}

resource "kubernetes_namespace" "app" {
  metadata {
    name = var.namespace
  }
}

resource "helm_release" "local_llm" {
  name          = "local-llm"
  chart         = pathexpand("${path.module}/${var.chart_path}")
  namespace     = kubernetes_namespace.app.metadata[0].name
  version       = "0.2.0"
  recreate_pods = true

  set {
    name  = "image.flavor"
    value = var.image_flavor
  }
  set {
    name  = "image.cpuDigest"
    value = var.cpu_image_digest
  }
  set {
    name  = "image.cudaDigest"
    value = var.cuda_image_digest
  }
  set {
    name  = "gateway.enabled"
    value = "true"
  }
  set {
    name  = "gateway.hostname"
    value = var.gateway_hostname
  }
  set {
    name  = "gateway.namespace"
    value = var.gateway_namespace
  }
  set {
    name  = "gateway.className"
    value = var.gateway_class_name
  }
  set {
    name  = "gateway.certificateSecretRef"
    value = var.tls_secret_name
  }
  set {
    name  = "gateway.apiKeySecretRef"
    value = var.api_key_secret_name
  }
  set {
    name  = "networkPolicy.allowedNamespaces[0]"
    value = var.gateway_namespace
  }
  set {
    name  = "serviceAccount.name"
    value = "local-llm"
  }
  set {
    name  = "serviceAccount.annotations.eks\\.amazonaws\\.com/role-arn"
    value = aws_iam_role.object_read.arn
  }

  depends_on = [module.eks, aws_s3_bucket.weights, aws_iam_policy.object_read, aws_iam_role_policy_attachment.object_read]
}

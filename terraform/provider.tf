
# ── Remote State ─────────────────────────────────────────────────────────────
# Partial backend config: bucket and region are passed at init time so no
# sensitive values are hardcoded here. The CD workflow calls:
#
#   terraform init \
#     -backend-config="bucket=$TF_STATE_BUCKET" \
#     -backend-config="region=us-west-2"
#
# Create the bucket once before your first apply:
#
#   aws s3api create-bucket \
#     --bucket <your-tfstate-bucket> \
#     --region us-west-2 \
#     --create-bucket-configuration LocationConstraint=us-west-2
#   aws s3api put-bucket-versioning \
#     --bucket <your-tfstate-bucket> \
#     --versioning-configuration Status=Enabled
#   aws s3api put-bucket-encryption \
#     --bucket <your-tfstate-bucket> \
#     --server-side-encryption-configuration \
#       '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'

terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "6.38.0"
    }
  }
  backend "s3" {
    key     = "ad-lambda/terraform.tfstate"
    encrypt = true
    region  = "us-east-1"
    # bucket and region supplied via -backend-config at init time
  }
}

# ── AWS Provider ─────────────────────────────────────────────────────────────
# Configures the region resources are deployed into (separate from the
# backend region above, which only controls where state is stored).
provider "aws" {
  region = var.aws_region
}

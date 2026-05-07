variable "vpc_cidr_block" {
  description = "CIDR block for the VPC"
  type        = string
  default     = "10.0.0.0/16"
}

variable "lambda_runtime" {
  description = "Runtime for the Lambda function"
  type        = string
  default     = "python3.11"
}

variable "directory_admin_password" {
  description = "Admin password for the AWS Managed Microsoft AD. Pass via -var flag or a .tfvars file — never commit this value."
  type        = string
  sensitive   = true
}

variable "onboarding_api_key" {
  description = "API key callers must supply in the x-api-key header to reach the onboarding endpoint."
  type        = string
  sensitive   = true
}

variable "use_mock_ldap" {
  description = "Set to \"true\" to skip real LDAP calls and log what would have been provisioned. Safe for demos and CI runs without an Active Directory."
  type        = string
  default     = "false"
}

variable "azure_sync_enabled" {
  description = "Set to \"true\" to sync users to Microsoft Entra ID after AD provisioning. Requires azure_tenant_id, azure_client_id, and azure_client_secret secrets to be populated."
  type        = string
  default     = "false"
}

variable "confidence_threshold" {
  description = "Initial confidence threshold written to SSM. Adjust the SSM parameter directly after deployment to tune without redeploying."
  type        = number
  default     = 0.8

  validation {
    condition     = var.confidence_threshold >= 0.0 && var.confidence_threshold <= 1.0
    error_message = "confidence_threshold must be between 0.0 and 1.0."
  }
}

variable "offboard_confidence_threshold" {
  description = "Initial confidence threshold for offboarding requests. Higher than the onboarding threshold (default 0.95) because offboarding disables accounts and is harder to undo. Adjust the SSM parameter directly after deployment."
  type        = number
  default     = 0.95

  validation {
    condition     = var.offboard_confidence_threshold >= 0.0 && var.offboard_confidence_threshold <= 1.0
    error_message = "offboard_confidence_threshold must be between 0.0 and 1.0."
  }
}

# Terraform Learning Lessons - AD Lambda Project

## Overview
This document summarizes the errors and mistakes encountered during the Terraform infrastructure setup for an AWS Lambda-based Active Directory onboarding system.

---

## Lesson 1: Variable Syntax

### ❌ Wrong
```terraform
variable "lambda_runtime" {
  default = ~> "python 3.9"   # Wrong: ~> is for provider versions, not variable defaults
}
```

### ✅ Correct
```terraform
variable "lambda_runtime" {
  default = "python3.9"       # Correct: no space, no ~>
}
```

### Key Takeaway
The `~>` operator is only for Terraform provider version constraints, not for variable defaults. Also, AWS Lambda runtimes use `python3.9` not `python 3.9` (no space).

---

## Lesson 2: CIDR Block Syntax

### ❌ Wrong
```terraform
variable "vpc_cidr_block" {
  default = "60.0.0.01/16"    # Wrong: invalid IP (01)
}
```

### ✅ Correct
```terraform
variable "vpc_cidr_block" {
  default = "60.0.0.0/16"     # Correct: valid CIDR notation
}
```

### Key Takeaway
Always validate CIDR block syntax. Numbers should not have leading zeros (e.g., use `0` not `01`).

---

## Lesson 3: Referencing Variables

### ❌ Wrong
```terraform
cidr_blocks = var.vpc_cidr_block   # Wrong: cidr_blocks expects a list
```

### ✅ Correct
```terraform
cidr_blocks = [var.vpc_cidr_block] # Correct: wrap in brackets for list
```

### Key Takeaway
Check the AWS resource attribute type requirements. Many attributes expect lists (e.g., `cidr_blocks`, `subnet_ids`) and require bracket notation.

---

## Lesson 4: Terraform Data Source Paths

### ❌ Wrong
```terraform
data "archive_file" "lambda_zip" {
  source_file = "Lambda_func.py"   # Wrong: looks in terraform/ folder
}
```

### ✅ Correct
```terraform
data "archive_file" "lambda_zip" {
  source_file = "../lambda-package/Lambda_func.py"  # Correct: relative path from terraform/ folder
}
```

### Key Takeaway
Terraform executes from the working directory (where you run `terraform init`). Use relative paths like `../` to reference files outside the Terraform folder.

---

## Lesson 5: JSON Syntax in jsonencode()

### ❌ Wrong
```terraform
assume_role_policy = jsonencode({
  Statement = [
    {
      Action = "sts:AssumeRole"
      Effect = "Allow"
    Principal = {           # Wrong: missing { after Effect
      Service = "lambda.amazonaws.com"
    }
  ]
})
```

### ✅ Correct
```terraform
assume_role_policy = jsonencode({
  Version = "2012-10-17"
  Statement = [
    {
      Action = "sts:AssumeRole"
      Effect = "Allow"
      Principal = {          # Correct: { after Effect
        Service = "lambda.amazonaws.com"
      }
    }
  ]
})
```

### Key Takeaway
When using `jsonencode()`, ensure every object has proper braces `{}`. The Terraform formatter doesn't always catch nested JSON syntax errors.

---

## Lesson 6: Resource Naming

### ❌ Wrong
```terraform
OPENAI_API_KEY = aws_secretsmanager_secret.api_secret.arn  # Wrong: resource name doesn't exist
```

### ✅ Correct
```terraform
OPENAI_API_KEY = aws_secretsmanager_secret.openapi_secret.arn  # Correct: matches resource name
```

### Key Takeaway
Always match the resource name defined in your `resource` block. In this case, the resource was named `openapi_secret`, not `api_secret`.

---

## Lesson 7: Resource Type References

### ❌ Wrong
```terraform
DYNAMODB_TABLE = aws_dynamodb.onboarding_request_table.name  # Wrong: using aws_dynamodb
```

### ✅ Correct
```terraform
DYNAMODB_TABLE = aws_dynamodb_table.onboarding_request_table.name  # Correct: using aws_dynamodb_table
```

### Key Takeaway
Terraform uses specific resource types. For DynamoDB tables, it's `aws_dynamodb_table`, not `aws_dynamodb`. Always verify the correct resource type in the AWS provider documentation.

---

## Lesson 8: Missing Closing Braces

### ❌ Wrong
```terraform
resource "aws_directory_service_directory" "bar" {
  name = "business.abc.com"
  vpc_settings {
    vpc_id = aws_vpc.main.id
  }
  tags = {                  # Missing closing }
}                           # Second } needed
```

### ✅ Correct
```terraform
resource "aws_directory_service_directory" "bar" {
  name = "business.abc.com"
  vpc_settings {
    vpc_id = aws_vpc.main.id
  }
  tags = {
    Project = "AD_Lambda_Onboarding"
  }
}                           # One closing } for resource
}
```

### Key Takeaway
Each resource block needs a closing `}`. Nested blocks (like `vpc_settings`, `tags`) also need their own closing `}`. Count your braces carefully.

---

## Summary Checklist

| # | Check | Command |
|---|-------|---------|
| 1 | Variable defaults use correct syntax | No `~>` in variable defaults |
| 2 | CIDR blocks are valid | No leading zeros in IPs |
| 3 | List attributes wrapped in brackets | `[var.name]` not `var.name` |
| 4 | File paths are relative to working dir | Use `../` for parent folders |
| 5 | JSON objects have matching braces | Count `{` and `}` |
| 6 | Resource names match references | Check `resource "type" "name"` |
| 7 | Use correct resource types | `aws_dynamodb_table`, not `aws_dynamodb` |
| 8 | All resource blocks closed | Every `{` needs a matching `}` |

---

## Useful Commands

```bash
# Validate Terraform syntax
terraform validate

# Format Terraform files
terraform fmt

# Plan and see changes
terraform plan

# Apply changes
terraform apply
```

---

*Generated: April 2026*
*Project: AD Lambda Onboarding System*
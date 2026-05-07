### ============================================================
### VPC & Networking
### ============================================================

resource "aws_vpc" "main" {
  cidr_block           = "10.0.0.0/16"
  enable_dns_support   = true
  enable_dns_hostnames = true
}

resource "aws_subnet" "foo" {
  vpc_id                  = aws_vpc.main.id
  availability_zone       = "us-west-2a"
  cidr_block              = "10.0.1.0/24"
  map_public_ip_on_launch = false
}

# Fixed: was 10.0.1.0/22, which overlapped with foo (10.0.1.0/24)
resource "aws_subnet" "bar" {
  vpc_id                  = aws_vpc.main.id
  availability_zone       = "us-west-2b"
  cidr_block              = "10.0.2.0/24"
  map_public_ip_on_launch = false
}

### Security group for Lambda functions
resource "aws_security_group" "lambda_sg" {
  name        = "lambda_sg"
  description = "Security group for Lambda functions"
  vpc_id      = aws_vpc.main.id
}

### Allow all egress within the VPC (reaches VPC endpoints and the AD directory)
resource "aws_security_group_rule" "lambda_egress_vpc" {
  type              = "egress"
  from_port         = 0
  to_port           = 0
  protocol          = "-1"
  security_group_id = aws_security_group.lambda_sg.id
  cidr_blocks       = [var.vpc_cidr_block]
  description       = "Allow Lambda to reach VPC endpoints and AD"
}

### Security group for VPC Interface Endpoints
resource "aws_security_group" "vpc_endpoint_sg" {
  name        = "vpc_endpoint_sg"
  description = "Security group for VPC interface endpoints"
  vpc_id      = aws_vpc.main.id
}

### Allow HTTPS inbound from Lambda so it can reach AWS service endpoints
resource "aws_security_group_rule" "endpoint_ingress_from_lambda" {
  type                     = "ingress"
  from_port                = 443
  to_port                  = 443
  protocol                 = "tcp"
  security_group_id        = aws_security_group.vpc_endpoint_sg.id
  source_security_group_id = aws_security_group.lambda_sg.id
  description              = "HTTPS from Lambda"
}


### ============================================================
### VPC Interface Endpoints (keep Lambda off the public internet)
### ============================================================

resource "aws_vpc_endpoint" "secretsmanager_endpoint" {
  vpc_id              = aws_vpc.main.id
  service_name        = "com.amazonaws.us-west-2.secretsmanager"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = [aws_subnet.foo.id]
  security_group_ids  = [aws_security_group.vpc_endpoint_sg.id]
  private_dns_enabled = true
}

resource "aws_vpc_endpoint" "dynamodb_endpoint" {
  vpc_id              = aws_vpc.main.id
  service_name        = "com.amazonaws.us-west-2.dynamodb"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = [aws_subnet.foo.id]
  security_group_ids  = [aws_security_group.vpc_endpoint_sg.id]
  private_dns_enabled = true
}

resource "aws_vpc_endpoint" "sns_endpoint" {
  vpc_id              = aws_vpc.main.id
  service_name        = "com.amazonaws.us-west-2.sns"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = [aws_subnet.foo.id]
  security_group_ids  = [aws_security_group.vpc_endpoint_sg.id]
  private_dns_enabled = true
}

### Needed for the onboarding Lambda to invoke the notify_sns Lambda from within the VPC
resource "aws_vpc_endpoint" "lambda_endpoint" {
  vpc_id              = aws_vpc.main.id
  service_name        = "com.amazonaws.us-west-2.lambda"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = [aws_subnet.foo.id]
  security_group_ids  = [aws_security_group.vpc_endpoint_sg.id]
  private_dns_enabled = true
}

### Needed for Claude (Bedrock Runtime) calls
resource "aws_vpc_endpoint" "bedrock_endpoint" {
  vpc_id              = aws_vpc.main.id
  service_name        = "com.amazonaws.us-west-2.bedrock-runtime"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = [aws_subnet.foo.id]
  security_group_ids  = [aws_security_group.vpc_endpoint_sg.id]
  private_dns_enabled = true
}


### ============================================================
### IAM — Lambda Execution Role
### ============================================================

resource "aws_iam_role" "lambda_role" {
  name = "lambda_role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
    }]
  })
}

### Basic execution: CloudWatch Logs
resource "aws_iam_role_policy_attachment" "lambda_role_basic" {
  role       = aws_iam_role.lambda_role.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

### Required for Lambda functions running inside a VPC
resource "aws_iam_role_policy_attachment" "lambda_role_vpc" {
  role       = aws_iam_role.lambda_role.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole"
}

resource "aws_iam_policy" "lambda_policy" {
  name = "lambda_policy"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "SecretsManagerReadLDAP"
        Effect = "Allow"
        Action = ["secretsmanager:GetSecretValue"]
        Resource = [
          aws_secretsmanager_secret.ldap_server_address.arn,
          aws_secretsmanager_secret.ldap_username.arn,
          aws_secretsmanager_secret.ldap_password.arn,
        ]
      },
      {
        Sid    = "SecretsManagerReadAzure"
        Effect = "Allow"
        Action = ["secretsmanager:GetSecretValue"]
        Resource = [
          aws_secretsmanager_secret.azure_tenant_id.arn,
          aws_secretsmanager_secret.azure_client_id.arn,
          aws_secretsmanager_secret.azure_client_secret.arn,
        ]
      },
      {
        Sid      = "DynamoDBWriteAuditLog"
        Effect   = "Allow"
        Action   = ["dynamodb:PutItem"]
        Resource = [aws_dynamodb_table.onboarding_request_table.arn]
      },
      {
        Sid      = "SNSPublishNotifications"
        Effect   = "Allow"
        Action   = ["sns:Publish"]
        Resource = [aws_sns_topic.notification_topic.arn]
      },
      {
        Sid      = "InvokeNotifySNSLambda"
        Effect   = "Allow"
        Action   = ["lambda:InvokeFunction"]
        # Wildcard avoids circular dependency; tighten with ARN after first apply
        Resource = ["arn:aws:lambda:us-west-2:*:function:notify_sns_function"]
      },
      {
        Sid      = "BedrockInvokeClaude"
        Effect   = "Allow"
        Action   = ["bedrock:InvokeModel"]
        Resource = ["*"]
      },
      {
        Sid      = "SSMReadConfidenceThresholds"
        Effect   = "Allow"
        Action   = ["ssm:GetParameter"]
        # Covers both /ad-lambda/confidence-threshold and
        # /ad-lambda/offboard-confidence-threshold
        Resource = [
          aws_ssm_parameter.confidence_threshold.arn,
          aws_ssm_parameter.offboard_confidence_threshold.arn,
        ]
      }
    ]
  })
}

### Attach custom policy to the Lambda execution role (previously was on an IAM User — fixed)
resource "aws_iam_role_policy_attachment" "lambda_role_custom" {
  role       = aws_iam_role.lambda_role.name
  policy_arn = aws_iam_policy.lambda_policy.arn
}


### ============================================================
### Secrets Manager
### ============================================================

resource "aws_secretsmanager_secret" "ldap_server_address" {
  name = "ldap_server_address"
}

resource "aws_secretsmanager_secret" "ldap_username" {
  name = "ldap_username"
}

resource "aws_secretsmanager_secret" "ldap_password" {
  name = "ldap_password"
}

### Azure AD / Entra ID credentials (populate after first apply if using Azure sync)
resource "aws_secretsmanager_secret" "azure_tenant_id" {
  name = "azure_tenant_id"
}

resource "aws_secretsmanager_secret" "azure_client_id" {
  name = "azure_client_id"
}

resource "aws_secretsmanager_secret" "azure_client_secret" {
  name = "azure_client_secret"
}

### Directory Service admin password (used by aws_directory_service_directory only)
resource "aws_secretsmanager_secret" "directory_admin_password" {
  name = "directory_admin_password"
}

resource "aws_secretsmanager_secret_version" "directory_admin_password_version" {
  secret_id     = aws_secretsmanager_secret.directory_admin_password.id
  secret_string = var.directory_admin_password
}


### ============================================================
### Lambda Packaging
### ============================================================

### Install pip dependencies into the lambda-package directory before zipping.
### Run: pip install -r ../lambda-package/requirements.txt -t ../lambda-package/
### Note: must be run on Linux x86_64 (or use --platform manylinux) to match Lambda runtime.
resource "null_resource" "pip_install" {
  triggers = {
    requirements = filemd5("${path.module}/../lambda-package/requirements.txt")
  }

  provisioner "local-exec" {
    command = "pip install -r ${path.module}/../lambda-package/requirements.txt -t ${path.module}/../lambda-package/ --quiet"
  }
}

### Zip the entire lambda-package directory (includes all .py files + installed deps)
data "archive_file" "lambda_zip" {
  type        = "zip"
  source_dir  = "../lambda-package"
  output_path = "lambda_function.zip"
  excludes    = ["__pycache__", "*.pyc", "*.pyo"]
  depends_on  = [null_resource.pip_install]
}


### ============================================================
### Lambda Functions
### ============================================================

### Main onboarding Lambda
resource "aws_lambda_function" "onboarding_function" {
  filename         = data.archive_file.lambda_zip.output_path
  function_name    = "onboarding_function"
  role             = aws_iam_role.lambda_role.arn
  handler          = "Lambda_func.lambda_handler"
  runtime          = var.lambda_runtime
  source_code_hash = data.archive_file.lambda_zip.output_base64sha256
  timeout          = 60

  vpc_config {
    subnet_ids         = [aws_subnet.foo.id, aws_subnet.bar.id]
    security_group_ids = [aws_security_group.lambda_sg.id]
  }

  environment {
    variables = {
      DOMAIN                 = aws_directory_service_directory.ad_directory.name
      BASE_DN                = "DC=business,DC=abc,DC=com"
      DYNAMODB_TABLE_NAME    = aws_dynamodb_table.onboarding_request_table.name
      SNS_TOPIC_ARN          = aws_sns_topic.notification_topic.arn
      NOTIFY_SNS_LAMBDA_NAME          = aws_lambda_function.notify_sns_function.function_name
      USE_MOCK_LDAP                   = var.use_mock_ldap
      AZURE_SYNC_ENABLED              = var.azure_sync_enabled
      CONFIDENCE_THRESHOLD_SSM_PARAM = aws_ssm_parameter.confidence_threshold.name
    }
  }

  depends_on = [aws_iam_role_policy_attachment.lambda_role_custom]
}

### Allow API Gateway to invoke the onboarding Lambda
resource "aws_lambda_permission" "apigw_invoke_onboarding" {
  statement_id  = "AllowAPIGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.onboarding_function.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.onboarding_api.execution_arn}/*/*"
}

### SNS notification Lambda (decoupled from main flow)
resource "aws_lambda_function" "notify_sns_function" {
  filename         = data.archive_file.lambda_zip.output_path
  function_name    = "notify_sns_function"
  role             = aws_iam_role.lambda_role.arn
  handler          = "Notify_SNS.lambda_handler"
  runtime          = var.lambda_runtime
  source_code_hash = data.archive_file.lambda_zip.output_base64sha256
  timeout          = 30

  vpc_config {
    subnet_ids         = [aws_subnet.foo.id, aws_subnet.bar.id]
    security_group_ids = [aws_security_group.lambda_sg.id]
  }

  environment {
    variables = {
      SNS_TOPIC_ARN = aws_sns_topic.notification_topic.arn
    }
  }

  depends_on = [aws_iam_role_policy_attachment.lambda_role_custom]
}



### Offboarding Lambda
resource "aws_lambda_function" "offboarding_function" {
  filename         = data.archive_file.lambda_zip.output_path
  function_name    = "offboarding_function"
  role             = aws_iam_role.lambda_role.arn
  handler          = "Offboard_func.lambda_handler"
  runtime          = var.lambda_runtime
  source_code_hash = data.archive_file.lambda_zip.output_base64sha256
  timeout          = 60

  vpc_config {
    subnet_ids         = [aws_subnet.foo.id, aws_subnet.bar.id]
    security_group_ids = [aws_security_group.lambda_sg.id]
  }

  environment {
    variables = {
      DOMAIN                                  = aws_directory_service_directory.ad_directory.name
      BASE_DN                                 = "DC=business,DC=abc,DC=com"
      GROUP_BASE_DN                           = "OU=Groups,DC=business,DC=abc,DC=com"
      DYNAMODB_TABLE_NAME                     = aws_dynamodb_table.onboarding_request_table.name
      SNS_TOPIC_ARN                           = aws_sns_topic.notification_topic.arn
      NOTIFY_SNS_LAMBDA_NAME                  = aws_lambda_function.notify_sns_function.function_name
      USE_MOCK_LDAP                           = var.use_mock_ldap
      AZURE_SYNC_ENABLED                      = var.azure_sync_enabled
      OFFBOARD_CONFIDENCE_THRESHOLD_SSM_PARAM = aws_ssm_parameter.offboard_confidence_threshold.name
    }
  }

  depends_on = [aws_iam_role_policy_attachment.lambda_role_custom]
}

### Allow API Gateway to invoke the offboarding Lambda
resource "aws_lambda_permission" "apigw_invoke_offboarding" {
  statement_id  = "AllowAPIGatewayInvokeOffboarding"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.offboarding_function.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.onboarding_api.execution_arn}/*"
}

### API key authorizer Lambda
resource "aws_lambda_function" "authorizer_function" {
  filename         = data.archive_file.lambda_zip.output_path
  function_name    = "onboarding_authorizer"
  role             = aws_iam_role.lambda_role.arn
  handler          = "api_authorizer.lambda_handler"
  runtime          = var.lambda_runtime
  source_code_hash = data.archive_file.lambda_zip.output_base64sha256
  timeout          = 5

  environment {
    variables = {
      ONBOARDING_API_KEY = var.onboarding_api_key
    }
  }

  depends_on = [aws_iam_role_policy_attachment.lambda_role_basic]
}

### Allow API Gateway to invoke the authorizer Lambda
resource "aws_lambda_permission" "apigw_invoke_authorizer" {
  statement_id  = "AllowAPIGatewayInvokeAuthorizer"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.authorizer_function.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.onboarding_api.execution_arn}/*"
}

### ============================================================
### API Gateway
### ============================================================

resource "aws_apigatewayv2_api" "onboarding_api" {
  name          = "onboarding_api"
  protocol_type = "HTTP"
}


### Lambda REQUEST authorizer — requires x-api-key header on all requests
resource "aws_apigatewayv2_authorizer" "api_key_authorizer" {
  api_id                            = aws_apigatewayv2_api.onboarding_api.id
  authorizer_type                   = "REQUEST"
  authorizer_uri                    = aws_lambda_function.authorizer_function.invoke_arn
  name                              = "api_key_authorizer"
  enable_simple_responses           = true
  authorizer_payload_format_version = "2.0"
  identity_sources                  = ["$request.header.x-api-key"]
}

### Use Lambda resource policy (aws_lambda_permission above) instead of IAM credentials
resource "aws_apigatewayv2_integration" "lambda_integration" {
  api_id             = aws_apigatewayv2_api.onboarding_api.id
  integration_type   = "AWS_PROXY"
  integration_uri    = aws_lambda_function.onboarding_function.invoke_arn
  depends_on         = [aws_lambda_function.onboarding_function]
}

resource "aws_apigatewayv2_route" "onboarding_route" {
  api_id             = aws_apigatewayv2_api.onboarding_api.id
  route_key          = "POST /onboard"
  target             = "integrations/${aws_apigatewayv2_integration.lambda_integration.id}"
  authorization_type = "CUSTOM"
  authorizer_id      = aws_apigatewayv2_authorizer.api_key_authorizer.id
}

resource "aws_apigatewayv2_integration" "offboard_integration" {
  api_id           = aws_apigatewayv2_api.onboarding_api.id
  integration_type = "AWS_PROXY"
  integration_uri  = aws_lambda_function.offboarding_function.invoke_arn
  depends_on       = [aws_lambda_function.offboarding_function]
}

resource "aws_apigatewayv2_route" "offboarding_route" {
  api_id             = aws_apigatewayv2_api.onboarding_api.id
  route_key          = "POST /offboard"
  target             = "integrations/${aws_apigatewayv2_integration.offboard_integration.id}"
  authorization_type = "CUSTOM"
  authorizer_id      = aws_apigatewayv2_authorizer.api_key_authorizer.id
}

resource "aws_apigatewayv2_stage" "default" {
  api_id      = aws_apigatewayv2_api.onboarding_api.id
  name        = "$default"
  auto_deploy = true
}


### ============================================================
### DynamoDB — Onboarding Audit Log
### ============================================================

resource "aws_dynamodb_table" "onboarding_request_table" {
  name         = "onboarding_request_table"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "request_id"

  attribute {
    name = "request_id"
    type = "S"
  }

  ttl {
    attribute_name = "ttl"
    enabled        = true
  }
}


### ============================================================
### SNS — Notification Topic
### ============================================================

resource "aws_sns_topic" "notification_topic" {
  name = "notification_topic"
}


### ============================================================
### SSM Parameters
### ============================================================

resource "aws_ssm_parameter" "confidence_threshold" {
  name        = "/ad-lambda/confidence-threshold"
  type        = "String"
  value       = tostring(var.confidence_threshold)
  description = "Confidence score (0.0-1.0) below which onboarding requests are routed to manual review. Adjust without redeploying Lambda."

  tags = {
    Project = "ad-lambda"
  }
}

resource "aws_ssm_parameter" "offboard_confidence_threshold" {
  name        = "/ad-lambda/offboard-confidence-threshold"
  type        = "String"
  value       = tostring(var.offboard_confidence_threshold)
  description = "Confidence score (0.0-1.0) below which offboarding requests are held for manual review. Higher than the onboarding threshold (default 0.95) because offboarding is destructive."

  tags = {
    Project = "ad-lambda"
  }
}


### ============================================================
### AWS Managed Microsoft AD
### ============================================================

resource "aws_directory_service_directory" "ad_directory" {
  name     = "business.abc.com"
  password = aws_secretsmanager_secret_version.directory_admin_password_version.secret_string
  size     = "Small"
  type     = "MicrosoftAD"

  vpc_settings {
    vpc_id     = aws_vpc.main.id
    subnet_ids = [aws_subnet.foo.id, aws_subnet.bar.id]
  }

  tags = {
    Project = "AD_Lambda_Onboarding"
  }
}


### ============================================================
### Outputs
### ============================================================

output "api_endpoint" {
  description = "API Gateway endpoint for onboarding requests"
  value       = aws_apigatewayv2_api.onboarding_api.api_endpoint
}

output "dynamodb_table_name" {
  value = aws_dynamodb_table.onboarding_request_table.name
}

output "sns_topic_arn" {
  value = aws_sns_topic.notification_topic.arn
}

output "ad_dns_name" {
  description = "DNS name of the Active Directory — use as ldap_server_address secret value"
  value       = aws_directory_service_directory.ad_directory.name
}

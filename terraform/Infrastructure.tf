### Resolve the AWS account ID at plan time — used to scope IAM resources
### to specific ARNs without hardcoding the account number.
data "aws_caller_identity" "current" {}

### The AD domain name is a known literal. Keeping it in a local (instead of
### referencing aws_directory_service_directory.ad_directory.name) means the
### Lambdas and API Gateway no longer wait ~30+ min for the directory to finish.
locals {
  ad_domain = "business.abc.com"

  # Mock mode never touches LDAP, so it doesn't need a directory. Skipping it
  # saves ~$88/month and 20-45 minutes on every fresh apply.
  create_directory = lower(var.use_mock_ldap) != "true"
}

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

### Allow all egress within the VPC (reaches the interface VPC endpoints and the AD directory).
### Uses the VPC's actual CIDR so it can never drift from a separately-set variable.
resource "aws_security_group_rule" "lambda_egress_vpc" {
  type              = "egress"
  from_port         = 0
  to_port           = 0
  protocol          = "-1"
  security_group_id = aws_security_group.lambda_sg.id
  cidr_blocks       = [aws_vpc.main.cidr_block]
  description       = "Allow Lambda to reach VPC interface endpoints and AD"
}

### DynamoDB (and S3) use a GATEWAY endpoint (Interface endpoints are also an option), whose traffic is routed to the
### service's public prefix list — addresses OUTSIDE the VPC CIDR. The egress
### rule above (VPC CIDR only) therefore blocks it, causing a connect timeout to
### dynamodb.<region>.amazonaws.com. Allow egress to the gateway prefix list.
resource "aws_security_group_rule" "lambda_egress_dynamodb" {
  type              = "egress"
  from_port         = 443
  to_port           = 443
  protocol          = "tcp"
  security_group_id = aws_security_group.lambda_sg.id
  prefix_list_ids   = [aws_vpc_endpoint.dynamodb_endpoint.prefix_list_id]
  description       = "Allow Lambda to reach DynamoDB via its gateway endpoint"
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
### Interface endpoints are placed in BOTH subnets/AZs so the Lambdas keep
### access to AWS services if one AZ has problems.
###
### No SQS endpoint: the DLQ on notify_sns_function is written by the Lambda
### service itself (using the execution role), not by function code inside the VPC.

resource "aws_vpc_endpoint" "secretsmanager_endpoint" {
  vpc_id              = aws_vpc.main.id
  service_name        = "com.amazonaws.us-west-2.secretsmanager"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = [aws_subnet.foo.id, aws_subnet.bar.id]
  security_group_ids  = [aws_security_group.vpc_endpoint_sg.id]
  private_dns_enabled = true
}

# DynamoDB (and S3) only support Gateway endpoints, not Interface.
# Gateway endpoints attach to route tables and do not support private DNS.
resource "aws_vpc_endpoint" "dynamodb_endpoint" {
  vpc_id            = aws_vpc.main.id
  service_name      = "com.amazonaws.us-west-2.dynamodb"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = [aws_vpc.main.main_route_table_id]
}

resource "aws_vpc_endpoint" "sns_endpoint" {
  vpc_id              = aws_vpc.main.id
  service_name        = "com.amazonaws.us-west-2.sns"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = [aws_subnet.foo.id, aws_subnet.bar.id]
  security_group_ids  = [aws_security_group.vpc_endpoint_sg.id]
  private_dns_enabled = true
}

### Needed for the onboarding Lambda to invoke the notify_sns Lambda from within the VPC
resource "aws_vpc_endpoint" "lambda_endpoint" {
  vpc_id              = aws_vpc.main.id
  service_name        = "com.amazonaws.us-west-2.lambda"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = [aws_subnet.foo.id, aws_subnet.bar.id]
  security_group_ids  = [aws_security_group.vpc_endpoint_sg.id]
  private_dns_enabled = true
}

### Needed for Claude (Bedrock Runtime) calls
resource "aws_vpc_endpoint" "bedrock_endpoint" {
  vpc_id              = aws_vpc.main.id
  service_name        = "com.amazonaws.us-west-2.bedrock-runtime"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = [aws_subnet.foo.id, aws_subnet.bar.id]
  security_group_ids  = [aws_security_group.vpc_endpoint_sg.id]
  private_dns_enabled = true
}

### Needed for onboarding_function to read the confidence threshold from SSM
### Parameter Store within the VPC. Without this, the in-VPC SSM call has no
### route to the service and hangs until the Lambda times out.
resource "aws_vpc_endpoint" "ssm_endpoint" {
  vpc_id              = aws_vpc.main.id
  service_name        = "com.amazonaws.us-west-2.ssm"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = [aws_subnet.foo.id, aws_subnet.bar.id]
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
        # The SNS topic is encrypted with a customer-managed KMS key; publishers
        # need these permissions on the key or sns:Publish fails with KMS AccessDenied.
        Sid      = "KMSForEncryptedSNSTopic"
        Effect   = "Allow"
        Action   = ["kms:GenerateDataKey", "kms:Decrypt"]
        Resource = [aws_kms_key.sns_topic_key.arn]
      },
      {
        Sid    = "InvokeNotifySNSLambda"
        Effect = "Allow"
        Action = ["lambda:InvokeFunction"]
        Resource = [
          "arn:aws:lambda:us-west-2:${data.aws_caller_identity.current.account_id}:function:notify_sns_function"
        ]
      },
      {
        Sid    = "BedrockInvokeClaude"
        Effect = "Allow"
        Action = ["bedrock:InvokeModel"]
        # Claude Haiku 4.5 via the US cross-region inference profile. The profile
        # is invoked by ARN, and it may route to the underlying foundation model
        # in any US region, so both must be permitted.
        Resource = [
          "arn:aws:bedrock:us-west-2:${data.aws_caller_identity.current.account_id}:inference-profile/us.anthropic.claude-haiku-4-5-20251001-v1:0",
          "arn:aws:bedrock:*::foundation-model/anthropic.claude-haiku-4-5-20251001-v1:0"
        ]
      },
      {
        # Used by the Lambda service to deliver failed async events to the DLQ.
        Sid      = "SQSSendDLQ"
        Effect   = "Allow"
        Action   = ["sqs:SendMessage"]
        Resource = [aws_sqs_queue.notify_dlq.arn]
      },
      {
        Sid    = "SSMReadConfidenceThresholds"
        Effect = "Allow"
        Action = ["ssm:GetParameter"]
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

### NOTE: set ldap_server_address to one of the directory's DNS IPs (see the
### ad_dns_ip_addresses output), not the domain name. The VPC uses
### AmazonProvidedDNS, which cannot resolve business.abc.com.
resource "aws_secretsmanager_secret" "ldap_server_address" {
  name                    = "ldap_server_address"
  recovery_window_in_days = 0
}

resource "aws_secretsmanager_secret" "ldap_username" {
  name                    = "ldap_username"
  recovery_window_in_days = 0
}

resource "aws_secretsmanager_secret" "ldap_password" {
  name                    = "ldap_password"
  recovery_window_in_days = 0
}

### Azure AD / Entra ID credentials (populate after first apply if using Azure sync)
resource "aws_secretsmanager_secret" "azure_tenant_id" {
  name                    = "azure_tenant_id"
  recovery_window_in_days = 0
}

resource "aws_secretsmanager_secret" "azure_client_id" {
  name                    = "azure_client_id"
  recovery_window_in_days = 0
}

resource "aws_secretsmanager_secret" "azure_client_secret" {
  name                    = "azure_client_secret"
  recovery_window_in_days = 0
}

### Directory Service admin password (used by aws_directory_service_directory only)
resource "aws_secretsmanager_secret" "directory_admin_password" {
  name                    = "directory_admin_password"
  recovery_window_in_days = 0
}

resource "aws_secretsmanager_secret_version" "directory_admin_password_version" {
  count         = local.create_directory ? 1 : 0
  secret_id     = aws_secretsmanager_secret.directory_admin_password.id
  secret_string = var.directory_admin_password
}


### ============================================================
### Lambda Packaging
### ============================================================

### Install pip dependencies into the lambda-package directory before zipping.
### Run: pip install -r ../lambda-package/requirements.txt -t ../lambda-package/
### Note: must be run on Linux x86_64 (or use --platform manylinux) to match Lambda runtime.
###
### FIRST APPLY ON A CLEAN CHECKOUT: archive_file below is read at plan time,
### before this null_resource runs, so the first zip can be missing deps.
### Run the pip install command above manually once before the first apply.
resource "null_resource" "pip_install" {
  triggers = {
    requirements = filemd5("${path.module}/../lambda-package/requirements.txt")
    # Re-run (and therefore re-zip via the dependent archive_file) whenever any
    # top-level handler .py changes. Without this, archive_file caches its result
    # because it depends_on this null_resource, and code edits never get packaged.
    source_code = sha1(join("", [for f in fileset("${path.module}/../lambda-package", "*.py") : filesha1("${path.module}/../lambda-package/${f}")]))
  }

  provisioner "local-exec" {
    command = "python -m pip install -r ${path.module}/../lambda-package/requirements.txt -t ${path.module}/../lambda-package/ --quiet"
  }
}

### Zip the entire lambda-package directory (includes all .py files + installed deps)
data "archive_file" "lambda_zip" {
  type        = "zip"
  source_dir  = "../lambda-package"
  output_path = "lambda_function.zip"
  excludes    = ["__pycache__", "*.pyc", "*.pyo"]
  # NOTE: no depends_on. A data source with depends_on caches its result and
  # won't re-archive when only source files change (even when the dependency is
  # replaced) — which silently ships stale Lambda code. Dependencies are vendored
  # into lambda-package (and pip_install still runs on requirements/code change),
  # so the archive can safely read current files on every plan.
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
      DOMAIN                         = local.ad_domain
      BASE_DN                        = "DC=business,DC=abc,DC=com"
      DYNAMODB_TABLE_NAME            = aws_dynamodb_table.onboarding_request_table.name
      SNS_TOPIC_ARN                  = aws_sns_topic.notification_topic.arn
      NOTIFY_SNS_LAMBDA_NAME         = aws_lambda_function.notify_sns_function.function_name
      USE_MOCK_LDAP                  = var.use_mock_ldap
      AZURE_SYNC_ENABLED             = var.azure_sync_enabled
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

  # Failed async invocations (after Lambda's 2 built-in retries) are sent
  # here so no notification loss goes undetected.
  dead_letter_config {
    target_arn = aws_sqs_queue.notify_dlq.arn
  }

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
      DOMAIN                                  = local.ad_domain
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

### (Removed: the x-api-key authorizer Lambda, its API Gateway authorizer and
### invoke permission. No route used it — auth is the Slack signature check
### inside slack_dispatch_function.)


### ============================================================
### API Gateway
### ============================================================

resource "aws_apigatewayv2_api" "onboarding_api" {
  name          = "onboarding_api"
  protocol_type = "HTTP"
}

### The /onboard route targets the thin slack_dispatch_function (defined in
### slack_dispatch.tf), which acks Slack within 3s and async-invokes
### onboarding_function. onboarding_function itself is unchanged.
resource "aws_apigatewayv2_integration" "lambda_integration" {
  api_id           = aws_apigatewayv2_api.onboarding_api.id
  integration_type = "AWS_PROXY"
  integration_uri  = aws_lambda_function.slack_dispatch_function.invoke_arn
  depends_on       = [aws_lambda_function.slack_dispatch_function]
}

### No API Gateway auth on /onboard OR /offboard: slack_dispatch_function
### authenticates the caller itself by verifying the Slack signing-secret HMAC
### over the raw body (X-Slack-Signature). Slack cannot sign requests with AWS
### SigV4, so AWS_IAM here would 403 every Slack request before the dispatcher
### runs. Both routes are authorization_type = "NONE"; the dispatcher rejects
### anything without a valid Slack signature.
resource "aws_apigatewayv2_route" "onboarding_route" {
  api_id             = aws_apigatewayv2_api.onboarding_api.id
  route_key          = "POST /onboard"
  target             = "integrations/${aws_apigatewayv2_integration.lambda_integration.id}"
  authorization_type = "NONE" # Slack dispatch function verifies the Slack signature
}

# /offboard, like /onboard, is fronted by the thin slack_dispatch_function: it
# verifies the Slack signing-secret HMAC, acks Slack within 3s, and async-invokes
# offboarding_function. The dispatcher picks the worker from the request path.
resource "aws_apigatewayv2_integration" "offboard_integration" {
  api_id           = aws_apigatewayv2_api.onboarding_api.id
  integration_type = "AWS_PROXY"
  integration_uri  = aws_lambda_function.slack_dispatch_function.invoke_arn
  depends_on       = [aws_lambda_function.slack_dispatch_function]
}

resource "aws_apigatewayv2_route" "offboarding_route" {
  api_id             = aws_apigatewayv2_api.onboarding_api.id
  route_key          = "POST /offboard"
  target             = "integrations/${aws_apigatewayv2_integration.offboard_integration.id}"
  authorization_type = "NONE" # Slack dispatch function verifies the Slack signature
}

resource "aws_apigatewayv2_stage" "default" {
  api_id      = aws_apigatewayv2_api.onboarding_api.id
  name        = "$default"
  auto_deploy = true
  access_log_settings {
    destination_arn = aws_cloudwatch_log_group.api_gateway_logs.arn
    format = jsonencode({
      requestId         = "$context.requestId"
      requestTime       = "$context.requestTime"
      httpMethod        = "$context.httpMethod"
      routeKey          = "$context.routeKey"
      status            = "$context.status"
      responseLength    = "$context.responseLength"
      integrationStatus = "$context.integrationStatus"
      sourceIp          = "$context.identity.sourceIp"
      userAgent         = "$context.identity.userAgent"
    })
  }

  # Throttle all routes: burst of 10 req/s, sustained 5 req/s.
  # Protects downstream Bedrock and LDAP from runaway callers, including
  # validly-signed ones. Tune via the stage — no Lambda redeployment needed.
  default_route_settings {
    throttling_burst_limit = 10
    throttling_rate_limit  = 5
  }
}

resource "aws_cloudwatch_log_group" "api_gateway_logs" {
  name              = "/aws/apigateway/onboarding_api"
  retention_in_days = 14
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
### SQS — Dead-Letter Queue for notify_sns_function
### ============================================================

### Captures failed async invocations of notify_sns_function so no
### notification loss goes undetected. Lambda retries twice on failure,
### then writes the event payload here. Retained for 14 days.
resource "aws_sqs_queue" "notify_dlq" {
  name                      = "notify-sns-dlq"
  message_retention_seconds = 1209600 # 14 days

  tags = {
    Project = "ad-lambda"
  }
}

### Alert when any message lands in the DLQ — indicates notify_sns_function
### failed all retries and a notification was lost. Fires within 1 minute.
resource "aws_cloudwatch_metric_alarm" "notify_dlq_alarm" {
  alarm_name        = "notify-sns-dlq-messages"
  alarm_description = "notify_sns_function exhausted all retries — one or more notifications were not delivered. Check the DLQ and CloudWatch Logs."
  namespace         = "AWS/SQS"
  metric_name       = "ApproximateNumberOfMessagesVisible"
  dimensions = {
    QueueName = aws_sqs_queue.notify_dlq.name
  }
  statistic           = "Sum"
  period              = 60
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"

  alarm_actions = [aws_sns_topic.notification_topic.arn]
  ok_actions    = [aws_sns_topic.notification_topic.arn]

  tags = {
    Project = "ad-lambda"
  }
}


### ============================================================
### SNS — Notification Topic
### ============================================================

resource "aws_sns_topic" "notification_topic" {
  name              = "notification_topic"
  kms_master_key_id = aws_kms_key.sns_topic_key.arn
}

### Key policy: the root statement keeps IAM in control (so the Lambda role's
### KMSForEncryptedSNSTopic statement takes effect), and the CloudWatch statement
### lets the DLQ alarm publish to the encrypted topic. The default key policy
### does not allow CloudWatch, so alarm notifications would be silently dropped.
resource "aws_kms_key" "sns_topic_key" {
  description             = "KMS key for SNS topic encryption"
  deletion_window_in_days = 7
  enable_key_rotation     = true

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "EnableIAMPolicies"
        Effect    = "Allow"
        Principal = { AWS = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:root" }
        Action    = "kms:*"
        Resource  = "*"
      },
      {
        Sid       = "AllowCloudWatchAlarmsToPublish"
        Effect    = "Allow"
        Principal = { Service = "cloudwatch.amazonaws.com" }
        Action    = ["kms:Decrypt", "kms:GenerateDataKey*"]
        Resource  = "*"
      }
    ]
  })
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

### Creation normally takes ~20-45 min (two domain controllers across two AZs).
### `size` is omitted: it only applies to SimpleAD/ADConnector. For MicrosoftAD,
### capacity is set by `edition` (Standard is the smaller/cheaper option).
### Only created when use_mock_ldap is not "true" (see local.create_directory).
resource "aws_directory_service_directory" "ad_directory" {
  count    = local.create_directory ? 1 : 0
  name     = local.ad_domain
  type     = "MicrosoftAD"
  password = aws_secretsmanager_secret_version.directory_admin_password_version[0].secret_string
  edition  = "Standard"

  vpc_settings {
    vpc_id     = aws_vpc.main.id
    subnet_ids = [aws_subnet.foo.id, aws_subnet.bar.id]
  }

  tags = {
    Project = "AD_Lambda_Onboarding"
  }

  timeouts {
    create = "90m" # provider default is 60m; slow creations occasionally exceed it
  }

  lifecycle {
    # Recreating the directory costs another ~30+ min. Remove this line
    # before intentionally running `terraform destroy`.
    prevent_destroy = true

    precondition {
      condition     = length(var.directory_admin_password) >= 8
      error_message = "directory_admin_password must be set (8+ characters) when use_mock_ldap is not \"true\"."
    }
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
  description = "Domain name of the Active Directory (not resolvable via AmazonProvidedDNS — use ad_dns_ip_addresses for LDAP). Null in mock mode."
  value       = one(aws_directory_service_directory.ad_directory[*].name)
}

output "ad_dns_ip_addresses" {
  description = "Domain controller / DNS IPs of the Active Directory — use one of these as the ldap_server_address secret value. Null in mock mode."
  value       = one(aws_directory_service_directory.ad_directory[*].dns_ip_addresses)
}

output "notify_dlq_arn" {
  description = "ARN of the DLQ for failed notify_sns_function invocations."
  value       = aws_sqs_queue.notify_dlq.arn
}

output "notify_dlq_alarm_name" {
  description = "CloudWatch alarm that fires when any message lands in the DLQ."
  value       = aws_cloudwatch_metric_alarm.notify_dlq_alarm.alarm_name
}
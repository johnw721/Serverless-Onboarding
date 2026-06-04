### ============================================================
### Slack Notifier — delivers SNS notifications to a Slack channel
### ============================================================
#
# Flow:  in-VPC Lambdas  ->  SNS notification_topic  ->  slack_notifier_function
#                                                          (outside the VPC, so
#                                                           it can reach the
#                                                           public internet)  ->  Slack incoming webhook
#
# The in-VPC Lambdas have no public egress (only VPC interface endpoints), so
# they cannot call hooks.slack.com directly. This function lives outside the
# VPC and gets default internet access, and SNS decouples the two halves.

variable "slack_webhook_url" {
  description = "Slack incoming webhook URL. Pass via -var or a .tfvars file — never commit this value."
  type        = string
  sensitive   = true
}

# ── Webhook stored as an SSM SecureString (not a plaintext env var) ──────────
resource "aws_ssm_parameter" "slack_webhook_url" {
  name        = "/ad-lambda/slack-webhook-url"
  type        = "SecureString"
  value       = var.slack_webhook_url
  description = "Slack incoming webhook URL consumed by slack_notifier_function."

  tags = {
    Project = "ad-lambda"
  }
}

# ── Dedicated least-privilege role (separate from the in-VPC lambda_role) ────
resource "aws_iam_role" "slack_notifier_role" {
  name = "slack_notifier_role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
    }]
  })

  tags = {
    Project = "ad-lambda"
  }
}

# Basic execution (CloudWatch Logs). No VPC policy — this function is not in a VPC.
resource "aws_iam_role_policy_attachment" "slack_notifier_basic" {
  role       = aws_iam_role.slack_notifier_role.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

# Read + decrypt only the one webhook parameter.
resource "aws_iam_role_policy" "slack_notifier_ssm" {
  name = "slack-notifier-ssm-read"
  role = aws_iam_role.slack_notifier_role.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ReadWebhookParameter"
        Effect   = "Allow"
        Action   = "ssm:GetParameter"
        Resource = aws_ssm_parameter.slack_webhook_url.arn
      },
      {
        Sid      = "DecryptSecureStringViaSSM"
        Effect   = "Allow"
        Action   = "kms:Decrypt"
        Resource = "*"
        Condition = {
          StringEquals = {
            "kms:ViaService" = "ssm.${var.aws_region}.amazonaws.com"
          }
        }
      }
    ]
  })
}

# ── The notifier Lambda (reuses the same deployment zip, different handler) ──
resource "aws_lambda_function" "slack_notifier_function" {
  filename         = data.archive_file.lambda_zip.output_path
  function_name    = "slack_notifier_function"
  role             = aws_iam_role.slack_notifier_role.arn
  handler          = "slack_notifier.lambda_handler"
  runtime          = var.lambda_runtime
  source_code_hash = data.archive_file.lambda_zip.output_base64sha256
  timeout          = 15

  # No vpc_config: this function needs public egress to reach hooks.slack.com.

  environment {
    variables = {
      SLACK_WEBHOOK_SSM_PARAM = aws_ssm_parameter.slack_webhook_url.name
    }
  }

  tags = {
    Project = "ad-lambda"
  }
}

# ── Subscribe the Lambda to the existing SNS notification topic ──────────────
resource "aws_sns_topic_subscription" "slack_notifier_sub" {
  topic_arn = aws_sns_topic.notification_topic.arn
  protocol  = "lambda"
  endpoint  = aws_lambda_function.slack_notifier_function.arn
}

# Allow SNS to invoke the function.
resource "aws_lambda_permission" "sns_invoke_slack_notifier" {
  statement_id  = "AllowSNSInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.slack_notifier_function.function_name
  principal     = "sns.amazonaws.com"
  source_arn    = aws_sns_topic.notification_topic.arn
}

output "slack_notifier_function_name" {
  description = "Name of the Lambda that posts SNS notifications to Slack"
  value       = aws_lambda_function.slack_notifier_function.function_name
}

output "notification_topic_arn" {
  description = "ARN of the SNS topic. Use with: aws sns publish --topic-arn <this>"
  value       = aws_sns_topic.notification_topic.arn
}

### ============================================================
### Slack Dispatch — fast 3s ack in front of the /onboard route
### ============================================================
#
# Slack slash commands need an HTTP response within 3 seconds. onboarding_function
# can take longer (Bedrock + provisioning), so this thin function fronts the
# route: it async-invokes onboarding_function and returns an instant ack. The
# real result reaches Slack later via SNS -> slack_notifier_function.
#
# Lives outside the VPC so it can reach the Lambda control-plane API to invoke
# the worker. onboarding_function and its tests are untouched.

# ── Dedicated least-privilege role ───────────────────────────────────────────
resource "aws_iam_role" "slack_dispatch_role" {
  name = "slack_dispatch_role"

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

resource "aws_iam_role_policy_attachment" "slack_dispatch_basic" {
  role       = aws_iam_role.slack_dispatch_role.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

# Allowed to invoke only the onboarding worker function.
resource "aws_iam_role_policy" "slack_dispatch_invoke" {
  name = "slack-dispatch-invoke-workers"
  role = aws_iam_role.slack_dispatch_role.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid    = "InvokeWorkers"
      Effect = "Allow"
      Action = "lambda:InvokeFunction"
      Resource = [
        aws_lambda_function.onboarding_function.arn,
        aws_lambda_function.offboarding_function.arn,
      ]
    }]
  })
}

# ── The dispatcher Lambda (same deployment zip, different handler) ───────────
resource "aws_lambda_function" "slack_dispatch_function" {
  filename         = data.archive_file.lambda_zip.output_path
  function_name    = "slack_dispatch_function"
  role             = aws_iam_role.slack_dispatch_role.arn
  handler          = "slack_dispatch.lambda_handler"
  runtime          = var.lambda_runtime
  source_code_hash = data.archive_file.lambda_zip.output_base64sha256
  timeout          = 10

  # No vpc_config: needs to reach the Lambda API to async-invoke the worker.

  environment {
    variables = {
      # The dispatcher routes /onboard and /offboard to the right worker.
      ONBOARDING_FUNCTION_NAME  = aws_lambda_function.onboarding_function.function_name
      OFFBOARDING_FUNCTION_NAME = aws_lambda_function.offboarding_function.function_name
      # Verifies X-Slack-Signature on every /onboard and /offboard request (the
      # routes have no API Gateway authorizer anymore).
      SLACK_SIGNING_SECRET = var.slack_signing_secret
    }
  }

  tags = {
    Project = "ad-lambda"
  }
}

# Let API Gateway invoke the dispatcher.
resource "aws_lambda_permission" "apigw_invoke_dispatch" {
  statement_id  = "AllowAPIGatewayInvokeDispatch"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.slack_dispatch_function.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.onboarding_api.execution_arn}/*/*"
}

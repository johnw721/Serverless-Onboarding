### ============================================================
### CloudWatch Dashboard — single-pane operational view
### ============================================================
#
# One dashboard with invocations, errors, duration and throttles for every
# Lambda, plus API Gateway traffic/latency and the notify DLQ depth. Open it at:
#   CloudWatch -> Dashboards -> ad-lambda-overview
# or use the dashboard_url output below.

resource "aws_cloudwatch_dashboard" "overview" {
  dashboard_name = "ad-lambda-overview"

  dashboard_body = jsonencode({
    widgets = [
      {
        type   = "text"
        x      = 0
        y      = 0
        width  = 24
        height = 2
        properties = {
          markdown = "# AD Lambda Onboarding — Operations\nInvocations, errors, latency and throttles across the pipeline. Use the time-range picker (top right) to zoom into your demo window."
        }
      },
      {
        type   = "metric"
        x      = 0
        y      = 2
        width  = 12
        height = 6
        properties = {
          title  = "Lambda Invocations"
          view   = "timeSeries"
          stacked = false
          region = var.aws_region
          stat   = "Sum"
          period = 60
          metrics = [
            ["AWS/Lambda", "Invocations", "FunctionName", aws_lambda_function.slack_dispatch_function.function_name],
            ["...", aws_lambda_function.onboarding_function.function_name],
            ["...", aws_lambda_function.offboarding_function.function_name],
            ["...", aws_lambda_function.notify_sns_function.function_name],
            ["...", aws_lambda_function.slack_notifier_function.function_name],
          ]
        }
      },
      {
        type   = "metric"
        x      = 12
        y      = 2
        width  = 12
        height = 6
        properties = {
          title  = "Lambda Errors"
          view   = "timeSeries"
          stacked = false
          region = var.aws_region
          stat   = "Sum"
          period = 60
          metrics = [
            ["AWS/Lambda", "Errors", "FunctionName", aws_lambda_function.slack_dispatch_function.function_name],
            ["...", aws_lambda_function.onboarding_function.function_name],
            ["...", aws_lambda_function.offboarding_function.function_name],
            ["...", aws_lambda_function.notify_sns_function.function_name],
            ["...", aws_lambda_function.slack_notifier_function.function_name],
          ]
        }
      },
      {
        type   = "metric"
        x      = 0
        y      = 8
        width  = 12
        height = 6
        properties = {
          title  = "Lambda Duration (p99, ms)"
          view   = "timeSeries"
          stacked = false
          region = var.aws_region
          stat   = "p99"
          period = 60
          metrics = [
            ["AWS/Lambda", "Duration", "FunctionName", aws_lambda_function.slack_dispatch_function.function_name],
            ["...", aws_lambda_function.onboarding_function.function_name],
            ["...", aws_lambda_function.offboarding_function.function_name],
            ["...", aws_lambda_function.notify_sns_function.function_name],
            ["...", aws_lambda_function.slack_notifier_function.function_name],
          ]
        }
      },
      {
        type   = "metric"
        x      = 12
        y      = 8
        width  = 12
        height = 6
        properties = {
          title  = "Lambda Throttles"
          view   = "timeSeries"
          stacked = false
          region = var.aws_region
          stat   = "Sum"
          period = 60
          metrics = [
            ["AWS/Lambda", "Throttles", "FunctionName", aws_lambda_function.slack_dispatch_function.function_name],
            ["...", aws_lambda_function.onboarding_function.function_name],
            ["...", aws_lambda_function.offboarding_function.function_name],
            ["...", aws_lambda_function.notify_sns_function.function_name],
            ["...", aws_lambda_function.slack_notifier_function.function_name],
          ]
        }
      },
      {
        type   = "metric"
        x      = 0
        y      = 14
        width  = 12
        height = 6
        properties = {
          title  = "API Gateway — Requests & Errors"
          view   = "timeSeries"
          stacked = false
          region = var.aws_region
          stat   = "Sum"
          period = 60
          metrics = [
            ["AWS/ApiGateway", "Count", "ApiId", aws_apigatewayv2_api.onboarding_api.id],
            [".", "4xx", "ApiId", aws_apigatewayv2_api.onboarding_api.id],
            [".", "5xx", "ApiId", aws_apigatewayv2_api.onboarding_api.id],
          ]
        }
      },
      {
        type   = "metric"
        x      = 12
        y      = 14
        width  = 12
        height = 6
        properties = {
          title  = "API Gateway — Latency (ms) & Notify DLQ Depth"
          view   = "timeSeries"
          stacked = false
          region = var.aws_region
          period = 60
          metrics = [
            ["AWS/ApiGateway", "Latency", "ApiId", aws_apigatewayv2_api.onboarding_api.id, { stat = "p99" }],
            ["AWS/SQS", "ApproximateNumberOfMessagesVisible", "QueueName", aws_sqs_queue.notify_dlq.name, { stat = "Maximum" }],
          ]
        }
      }
    ]
  })
}

output "dashboard_url" {
  description = "Direct link to the CloudWatch dashboard"
  value       = "https://${var.aws_region}.console.aws.amazon.com/cloudwatch/home?region=${var.aws_region}#dashboards/dashboard/${aws_cloudwatch_dashboard.overview.dashboard_name}"
}

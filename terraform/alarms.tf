### ============================================================
### CloudWatch Alarms — onboarding_function health
### ============================================================
#
# These complement the existing notify-DLQ alarm. Both fire to the SNS
# notification_topic, so an alarm also flows out to Slack via
# slack_notifier_function. Tune thresholds via Terraform — no Lambda redeploy.

# Any error from the onboarding worker within a 5-minute window.
resource "aws_cloudwatch_metric_alarm" "onboarding_errors" {
  alarm_name          = "onboarding-function-errors"
  alarm_description   = "onboarding_function returned one or more errors. Check CloudWatch Logs for the failing request."
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  dimensions = {
    FunctionName = aws_lambda_function.onboarding_function.function_name
  }
  statistic           = "Sum"
  period              = 300
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

# p99 latency above 10s sustained for 2 consecutive minutes. Onboarding does
# Bedrock + LDAP, so this catches genuine slowdowns without flagging normal runs.
resource "aws_cloudwatch_metric_alarm" "onboarding_latency_p99" {
  alarm_name          = "onboarding-function-latency-p99"
  alarm_description   = "onboarding_function p99 duration exceeded 10s for 2 minutes. Investigate Bedrock or LDAP latency."
  namespace           = "AWS/Lambda"
  metric_name         = "Duration"
  dimensions = {
    FunctionName = aws_lambda_function.onboarding_function.function_name
  }
  extended_statistic  = "p99"
  period              = 60
  evaluation_periods  = 2
  threshold           = 10000 # milliseconds
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"

  alarm_actions = [aws_sns_topic.notification_topic.arn]
  ok_actions    = [aws_sns_topic.notification_topic.arn]

  tags = {
    Project = "ad-lambda"
  }
}

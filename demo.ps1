<#
.SYNOPSIS
  One-shot demo driver for the AD Lambda Onboarding system.

.DESCRIPTION
  Runs terraform plan + apply, fires a sample onboarding request through the
  API (which acks instantly and posts the result to Slack), then prints the
  CloudWatch dashboard URL to screenshot. Designed to be the single command you
  run on camera.

  Assumes `terraform init` has already been run (state backend configured) and
  that secrets.auto.tfvars exists with slack_webhook_url, onboarding_api_key,
  directory_admin_password, and use_mock_ldap.

.PARAMETER ApiKey
  The onboarding_api_key value (same as in secrets.auto.tfvars). Required so the
  script can call the API. If omitted, the script falls back to publishing
  directly to SNS instead of calling the API.

.PARAMETER Request
  The natural-language onboarding request to send.

.PARAMETER SkipApply
  Skip plan/apply and just fire the request (use when already deployed).

.EXAMPLE
  ./demo.ps1 -ApiKey "my-secret-key"

.EXAMPLE
  ./demo.ps1 -SkipApply -ApiKey "my-secret-key" -Request "Onboard Priya Patel as a DevOps Engineer"
#>

[CmdletBinding()]
param(
    [string]$ApiKey,
    [string]$Request = "Please onboard Sarah Chen as a Data Scientist starting Monday.",
    [switch]$SkipApply
)

$ErrorActionPreference = "Stop"
$tf = Join-Path $PSScriptRoot "terraform"

function Write-Step($msg) { Write-Host "`n=== $msg ===" -ForegroundColor Cyan }

Push-Location $tf
try {
    if (-not $SkipApply) {
        Write-Step "terraform plan"
        terraform plan -out=main-plan-v1

        Write-Step "terraform apply"
        terraform apply main-plan-v1
    }
    else {
        Write-Host "Skipping plan/apply (-SkipApply set)." -ForegroundColor Yellow
    }

    Write-Step "Reading outputs"
    $apiEndpoint = terraform output -raw api_endpoint
    $topicArn    = terraform output -raw notification_topic_arn
    $dashboard   = terraform output -raw dashboard_url
    Write-Host "API endpoint : $apiEndpoint"
    Write-Host "SNS topic    : $topicArn"

    Write-Step "Firing sample onboarding request"
    if ($ApiKey) {
        # Full path: API Gateway -> dispatcher (instant ack) -> worker -> SNS -> Slack
        $body = @{ request = $Request } | ConvertTo-Json
        $resp = Invoke-RestMethod -Method Post -Uri "$apiEndpoint/onboard?x-api-key=$ApiKey" `
            -ContentType "application/json" -Body $body
        Write-Host "Immediate ack from API:" -ForegroundColor Green
        $resp | ConvertTo-Json
        Write-Host "`nThe processed result will appear in your Slack channel in a few seconds." -ForegroundColor Green
    }
    else {
        Write-Host "No -ApiKey provided; publishing directly to SNS instead." -ForegroundColor Yellow
        aws sns publish --topic-arn $topicArn `
            --subject "Onboarding complete" `
            --message "User schen provisioned. Confidence 0.94 (threshold 0.80)."
        Write-Host "Notification published; check your Slack channel." -ForegroundColor Green
    }

    Write-Step "Screenshot your metrics here"
    Write-Host $dashboard -ForegroundColor White
    Write-Host "`nTip: set the dashboard time range to 'Last 1 hour' to frame the demo window."
    Write-Host "Reminder: run 'terraform destroy' after recording to stop charges." -ForegroundColor Yellow
}
finally {
    Pop-Location
}

<#
.SYNOPSIS
  One-shot demo driver for the AD Lambda Onboarding system.

.DESCRIPTION
  Runs terraform plan + apply, fires a Slack-signed onboarding request at the
  API (which acks instantly and posts the result to Slack), then prints the
  CloudWatch dashboard URL to screenshot. Designed to be the single command you
  run on camera.

  Assumes `terraform init` has already been run (state backend configured) and
  that terraform/secrets.auto.tfvars exists with slack_webhook_url,
  slack_signing_secret, and use_mock_ldap (plus directory_admin_password when
  use_mock_ldap is "false").

  The /onboard route only accepts requests signed with the Slack signing
  secret, so the script reads slack_signing_secret from secrets.auto.tfvars and
  signs the request the same way Slack does. If no signing secret is found, it
  falls back to publishing a sample notification directly to SNS.

.PARAMETER Request
  The natural-language onboarding request to send. Include a department
  (e.g. "in Engineering") or the request is routed to manual review.

.PARAMETER SkipApply
  Skip plan/apply and just fire the request (use when already deployed).

.EXAMPLE
  ./demo.ps1

.EXAMPLE
  ./demo.ps1 -SkipApply -Request "Onboard Priya Patel as a Software Engineer in Engineering"
#>

[CmdletBinding()]
param(
    [string]$Request = "Please onboard Sarah Chen as a Data Scientist in the Data Science department, starting Monday.",
    [switch]$SkipApply
)

$ErrorActionPreference = "Stop"
$tf = Join-Path $PSScriptRoot "terraform"

function Write-Step($msg) { Write-Host "`n=== $msg ===" -ForegroundColor Cyan }

function Get-SigningSecret {
    $path = Join-Path $tf "secrets.auto.tfvars"
    if (-not (Test-Path $path)) { return $null }
    $match = Select-String -Path $path -Pattern '^\s*slack_signing_secret\s*=\s*"([^"]+)"' | Select-Object -First 1
    if ($match) { return $match.Matches[0].Groups[1].Value }
    return $null
}

function Send-SlackSignedRequest($uri, $command, $text, $secret) {
    # Slack's scheme: X-Slack-Signature = "v0=" + HMAC-SHA256(secret, "v0:<timestamp>:<raw body>")
    $ts   = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
    $body = "command=$command&text=" + [uri]::EscapeDataString($text)
    $hmac = New-Object System.Security.Cryptography.HMACSHA256
    $hmac.Key = [Text.Encoding]::UTF8.GetBytes($secret)
    $hash = $hmac.ComputeHash([Text.Encoding]::UTF8.GetBytes("v0:${ts}:$body"))
    $sig  = "v0=" + (($hash | ForEach-Object { $_.ToString("x2") }) -join "")
    Invoke-RestMethod -Method Post -Uri $uri `
        -ContentType "application/x-www-form-urlencoded" `
        -Headers @{ "X-Slack-Request-Timestamp" = "$ts"; "X-Slack-Signature" = $sig } `
        -Body $body
}

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
    $secret = Get-SigningSecret
    if ($secret) {
        # Full path: API Gateway -> dispatcher (signature check + instant ack) -> worker -> SNS -> Slack
        $resp = Send-SlackSignedRequest "$apiEndpoint/onboard" "/onboard" $Request $secret
        Write-Host "Immediate ack from API:" -ForegroundColor Green
        $resp | ConvertTo-Json
        Write-Host "`nThe processed result will appear in your Slack channel in a few seconds." -ForegroundColor Green
    }
    else {
        Write-Host "No slack_signing_secret found in secrets.auto.tfvars; publishing directly to SNS instead." -ForegroundColor Yellow
        aws sns publish --topic-arn $topicArn `
            --subject "Onboarding complete" `
            --message "User schen provisioned. Confidence 0.94 (threshold 0.80)."
        Write-Host "Notification published; check your Slack channel." -ForegroundColor Green
    }

    Write-Step "Screenshot your metrics here"
    Write-Host $dashboard -ForegroundColor White
    Write-Host "`nTip: set the dashboard time range to 'Last 1 hour' to frame the demo window."
    Write-Host "Reminder: run 'terraform destroy' after recording to stop charges (see docs/DEMO_GUIDE.md section 8)." -ForegroundColor Yellow
}
finally {
    Pop-Location
}

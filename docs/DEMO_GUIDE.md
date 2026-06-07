# Demo Guide — AD Lambda Onboarding

End-to-end runbook: from a clean machine to recorded demo video and screenshot
artifacts. Every step lists the command and what it does. Commands are
PowerShell (Windows); the `terraform` and `aws` commands are identical on
macOS/Linux.

---

## 0. Prerequisites (one-time)

| Requirement | Why |
|---|---|
| AWS account with **Amazon Bedrock access to Claude Haiku 4.5** in your region | The onboarding Lambda calls Claude to parse requests. See "Enable the model" below — it's a one-time, per-account step, not a separate account or signup. |
| **AWS CLI** configured (`aws configure`) | Terraform and the test commands authenticate through it |
| **Terraform ≥ 1.5** | Provisions all infrastructure |
| **Slack app** with an **Incoming Webhook** URL | Receives result notifications |
| An **S3 bucket** for Terraform remote state | Backend storage (created once, below) |

> **Regions:** resources deploy to **us-west-2** (the project hardcodes its
> availability zones, VPC endpoints, and ARNs there — change `var.aws_region`
> only if you also update those in `Infrastructure.tf`). The Terraform **state
> bucket** is independent and can live anywhere; this guide uses us-east-1 to
> match the existing backend config. Make sure Bedrock access to **Claude
> Haiku 4.5** is in place in **us-west-2** (see below).

> **Do recruiters need to do this?** No. Reviewers watch the demo video and
> review the screenshots/artifacts — they don't redeploy. This Bedrock step
> only applies to *you* (or another engineer) reproducing the stack from scratch.

### Enable the model (one-time, per AWS account)

There's **no separate Bedrock account or signup** — Bedrock is a service inside
your existing AWS account. Serverless foundation models now auto-enable on first
invocation, but **Anthropic models require a one-time AWS Marketplace
subscription** that the Lambda's least-privilege role intentionally can't perform
itself. Complete it once as an admin:

1. Bedrock console → **us-west-2** → **Model catalog** → **Claude Haiku 4.5** →
   **Open in playground**.
2. Send any single prompt. This completes the subscription **account-wide**.
   (First-ever Anthropic use may also prompt a short "use case details" form.)
   **Your login identity must have Marketplace permissions to do this** — if you
   get `AccessDenied … aws-marketplace:Subscribe`, attach the AWS-managed policy
   **`AWSMarketplaceManageSubscriptions`** to your user/role (or use an
   `AdministratorAccess`/root identity), then retry. This is the *caller's*
   identity, not the Lambda role — never give the Lambda role Marketplace access.
3. Wait ~2 minutes for propagation, then continue. No redeploy is needed — the
   Terraform already grants the Lambda `bedrock:InvokeModel` on the model.

> The model is pinned in `lambda-package/bedrock_agent.py` as the US inference
> profile `us.anthropic.claude-haiku-4-5-20251001-v1:0`. To use a different
> active model, change it there (and the matching ARNs in `Infrastructure.tf`),
> or override with a `BEDROCK_MODEL_ID` env var on `onboarding_function`.

Create the state bucket once (skip if it already exists):

```powershell
aws s3api create-bucket --bucket <your-tfstate-bucket> --region us-east-1
aws s3api put-bucket-versioning --bucket <your-tfstate-bucket> `
  --versioning-configuration Status=Enabled
```
*Creates the versioned bucket Terraform stores its state file in.*

---

## 1. Configure secrets

Create `terraform/secrets.auto.tfvars` (auto-loaded by Terraform, already
git-ignored — **never commit it**):

```hcl
slack_webhook_url        = "https://hooks.slack.com/services/T000/B000/xxxxxxxx"
directory_admin_password = "Ch00se-A-Str0ng-Passw0rd!"
onboarding_api_key       = "pick-a-long-random-string"
slack_signing_secret     = "from-slack-app-basic-information"
use_mock_ldap            = "true"
```

- `slack_webhook_url` — the Incoming Webhook URL **Slack gave you** (outbound: app → Slack).
- `onboarding_api_key` — any secret; required by the `/offboard` route (sent as `x-api-key`).
- `slack_signing_secret` — your Slack app's **Signing Secret** (Basic Information -> App Credentials); `slack_dispatch_function` verifies every `/onboard` request against it.
- `use_mock_ldap = "true"` — skips real Active Directory so the full Claude pipeline runs at **zero directory cost**. Ideal for the demo.

---

## 2. Deploy

```powershell
cd terraform

terraform init `
  -backend-config="bucket=<your-tfstate-bucket>" `
  -backend-config="region=us-east-1"
```
*Downloads providers and connects to remote state.*

```powershell
terraform fmt
terraform validate
```
*`fmt` normalizes formatting; `validate` checks the config compiles. Fix any errors before continuing.*

```powershell
terraform plan -out=main-plan-v1
```
*Previews changes and saves the plan. Expect `Plan: N to add`. **Screenshot this** — it's your IaC artifact.*

> **Where `use_mock_ldap` is set:** it's a Terraform variable, not a separate
> command. The `secrets.auto.tfvars` from section 1 is auto-loaded, so the plan
> above already picks it up. If you're **not** using that file, pass it (and the
> other no-default vars) inline instead:
> ```powershell
> terraform plan -out=main-plan-v1 `
>   -var="use_mock_ldap=true" `
>   -var="directory_admin_password=..." `
>   -var="onboarding_api_key=..."
> ```
> Either way it flows into the Lambda's `USE_MOCK_LDAP` env var and takes effect on the next `apply`.

```powershell
terraform apply main-plan-v1
```
*Provisions everything. Ends with `Apply complete!` and prints outputs. **Screenshot the outputs.***

Grab the values you'll need:

```powershell
terraform output -raw api_endpoint
terraform output -raw notification_topic_arn
terraform output -raw dashboard_url
```
*Your API URL, the SNS topic ARN (for the quick test), and a direct link to the CloudWatch dashboard.*

---

## 3. Wire up Slack (for the full round trip)

In <https://api.slack.com/apps> → your app → **Slash Commands** → Create New Command:

- **Command:** `/onboard`
- **Request URL:** `<api_endpoint>/onboard`
  *(No query string. `slack_dispatch_function` authenticates the request by verifying Slack's `X-Slack-Signature` header against your `slack_signing_secret`.)*
- Save, then reinstall the app to your workspace if prompted.

Repeat for the offboard command (same dispatcher + signing-secret auth):

- **Command:** `/offboard`
- **Request URL:** `<api_endpoint>/offboard`
  *(No query string. `slack_dispatch_function` routes `/offboard` to `offboarding_function` and verifies the same `slack_signing_secret`.)*
- Save (and reinstall if prompted).

---

## 4. Run the demo

### Option 0 — one command (recommended for recording)

After `terraform init` and `secrets.auto.tfvars` are in place, run from the project root:

```powershell
./demo.ps1 -ApiKey "<your onboarding_api_key>"
```
*Runs plan → apply → fires a sample onboarding request → prints the dashboard URL. Add `-SkipApply` once deployed to just fire a request. Use `-Request "..."` to change the text.*

### Option A — quickest (outbound notification only)

```powershell
aws sns publish --topic-arn (terraform output -raw notification_topic_arn) `
  --subject "Onboarding complete" `
  --message "User schen provisioned. Confidence 0.94 (threshold 0.80)."
```
*Publishes straight to SNS → `slack_notifier_function` → a message appears in your Slack channel within ~2s. No Slack slash-command setup needed.*

### Option B — full round trip (most impressive)

In Slack, type:

```
/onboard Please onboard Sarah Chen as a Data Scientist starting Monday.
```

You'll see an instant **"⏳ Working on it…"** ack, then a few seconds later the
**result message** posts back into the channel. Flow:
`Slack → API Gateway → dispatcher (ack) → onboarding worker (Claude) → SNS → Slack`.

### Option C — API call (curl/Postman)

```powershell
# /onboard is authenticated by Slack signing-secret HMAC, so sign the body the way Slack does:
$api    = terraform output -raw api_endpoint
$secret = (Select-String -Path .\secrets.auto.tfvars -Pattern '^slack_signing_secret').Line -replace '.*=\s*"([^"]+)".*','$1'
$ts   = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
$body = "command=/onboard&text=" + [uri]::EscapeDataString("Please onboard Sarah Chen as a Data Scientist in Engineering.")
$h = New-Object System.Security.Cryptography.HMACSHA256
$h.Key = [Text.Encoding]::UTF8.GetBytes($secret)
$sig = "v0=" + (($h.ComputeHash([Text.Encoding]::UTF8.GetBytes("v0:$ts`:$body")) | ForEach-Object { $_.ToString("x2") }) -join "")
curl.exe -s -X POST "$api/onboard" -H "Content-Type: application/x-www-form-urlencoded" `
  -H "X-Slack-Request-Timestamp: $ts" -H "X-Slack-Signature: $sig" --data $body
```
*Returns the `Working on it…` ack immediately; the result lands in Slack. A bare `x-api-key` call no longer works on `/onboard` — only `/offboard` uses the API key.*
*More sample payloads (manual-review, validation errors, injection rejection) are in `sample_requests.md`.*

---

## 4b. Troubleshooting

Check the worker logs first — they pinpoint every case below:
```powershell
aws logs tail /aws/lambda/onboarding_function --since 15m --region us-west-2 --format short
```

| Symptom | Cause | Fix |
|---|---|---|
| Slack: **"/onboard failed because the app did not respond"** | `slack_dispatch_function` rejected the request (401) or didn't respond in time. With signing-secret auth the usual causes are: `slack_signing_secret` unset or different from the Slack app's value (log shows `signature mismatch`), or a stale `?x-api-key=` left on the Request URL. HTTP API preserves request-header **casing**, so the verifier must look up `X-Slack-Signature` case-insensitively. | Set `slack_signing_secret` in `secrets.auto.tfvars` to the app's **Signing Secret**, `terraform apply`, and set the Request URL to `<api_endpoint>/onboard` (no query string). Inspect `aws logs tail /aws/lambda/slack_dispatch_function --since 5m --region us-west-2 --format short`. |
| Ack appears, but **no result** posts back | The worker bailed before notifying. Common: `Failed to parse onboarding request … The provided model identifier is invalid` (wrong Bedrock model ID) or `AccessDenied … AWS Marketplace actions` (model not subscribed). | Confirm the model ID in `bedrock_agent.py` is the full inference profile `us.anthropic.claude-haiku-4-5-20251001-v1:0`, and complete the one-time model subscription (section 0, "Enable the model"). |
| Worker runs to **`Status: timeout`** (~60s) | A Bedrock call hung against a not-yet-subscribed/throttled model. | Same model-subscription fix. The Bedrock client is bounded (`read_timeout=10`, `max_attempts=2`) so callers' template fallbacks engage instead of timing out — redeploy if you changed it. |
| **Silent 60s timeout** with no `[ERROR]` line (or logs ending at `log_onboarding_request`) | An in-VPC AWS call had no route and no timeout bound. Two known causes: missing **SSM** interface endpoint (confidence-threshold read), and the **DynamoDB gateway** endpoint being blocked by the Lambda security group (gateway traffic targets a public prefix list outside the VPC CIDR). | Both are fixed in `Infrastructure.tf`: `aws_vpc_endpoint.ssm_endpoint`, and `aws_security_group_rule.lambda_egress_dynamodb` allowing egress to `dynamodb_endpoint.prefix_list_id`. The SSM and DynamoDB clients are also bounded so failures degrade fast. If you see `Connect timeout on endpoint URL ... dynamodb...`, the egress rule is missing. |
| `terraform apply` shows **`0 changed`** after you edited Lambda code | `archive_file` data sources with `depends_on` cache their hash in state and don't re-zip on source changes — silently shipping stale code. | The `depends_on` was removed and a `source_code` trigger added to `pip_install`. If it ever recurs, force it: `terraform state rm data.archive_file.lambda_zip` then `terraform apply`. Verify with `aws lambda get-function-configuration --function-name onboarding_function --query LastModified`. |
| `AccessDenied … aws-marketplace:Subscribe` even **from the console/playground** | The Anthropic model needs a one-time Marketplace subscription, and the **identity you're logged in as** lacks Marketplace permissions to complete it (this is your own user/role, *not* the Lambda role). | Attach the AWS-managed policy **`AWSMarketplaceManageSubscriptions`** to your login identity (or use an `AdministratorAccess`/root identity), then invoke Claude Haiku 4.5 once in the playground to subscribe account-wide. If even that is denied, an Organization SCP is blocking it — ask your AWS org admin to subscribe the model once. Never add Marketplace permissions to the Lambda role. |
| Slack: **`dispatch_unknown_error`** | Slack-side generic error — it didn't get a clean response from the endpoint. Usually transient. | Confirm the dispatcher ran (`aws logs tail /aws/lambda/slack_dispatch_function --since 15m --region us-west-2 --format short`); if it's healthy, just retry the command. |
| Raw alarm JSON dumped in Slack | The SNS topic is shared by CloudWatch alarms and onboarding results. | Expected; `slack_notifier.py` now formats alarms into a single readable line. |
| Onboard unexpectedly routed to **Pending Review** | The department was unstated or didn't resolve to a known group, so group assignment returns confidence 0.0 by design (it never guesses a department from the job title). | State the department explicitly (e.g. "...in Finance"). Known departments map deterministically; unknown ones fall to Claude and may still need review. See `resolve_department_group` in `helpers.py`. |

---

## 5. Run the test suite (a strong artifact on its own)

```powershell
cd ..
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r lambda-package/requirements.txt
pip install pytest moto
python -m pytest -v
```
*Runs unit + end-to-end (moto-mocked AWS) tests in an isolated virtualenv (`.venv/` is git-ignored). Using a venv avoids permission errors when writing console scripts into a system Python install. **Screenshot the green pass summary** — it shows tests, mocking, and CI discipline.*

> If you prefer not to use a venv and hit a `py.test.exe` write error, the
> packages still install correctly — just run tests with `python -m pytest -v`,
> which doesn't depend on the failing console-script wrapper.

---

## 6. Where to find the artifacts (what to screenshot)

| Artifact | Where to find it | Why it matters |
|---|---|---|
| **IaC plan/apply** | Terminal output of `terraform plan` / `apply` | Proves infrastructure-as-code end to end |
| **CloudWatch dashboard** | `terraform output -raw dashboard_url`, or CloudWatch → Dashboards → `ad-lambda-overview` | One screen: invocations, errors, p99 latency, throttles, API Gateway traffic/latency, DLQ depth |
| **Alarms (healthy)** | CloudWatch → Alarms — `onboarding-function-errors`, `onboarding-function-latency-p99`, `notify-sns-dlq-messages` all in **OK** | Shows you design for failure, not just the happy path |
| **DLQ at zero** | SQS → `notify_dlq` → Monitoring (0 messages) | Confirms reliable async delivery |
| **Structured logs** | CloudWatch → Log groups → `/aws/lambda/onboarding_function` — find the line with the confidence score vs. threshold | Observability + the AI decision logic |
| **Slack result** | Your Slack channel | The user-facing outcome |
| **Tests passing** | Terminal `pytest` output | Quality engineering signal |

> Capture the dashboard **after** running a few requests so the graphs have
> data. Set the time-range picker (top-right) to **Last 1 hour** to frame the
> demo window.

---

## 7. Demo video script (~3 minutes)

1. **Intro (15s)** — "This is an AI-driven AD onboarding system: a Slack command provisions an Active Directory account, with Claude parsing the request and a confidence gate before any change is made. All infrastructure is Terraform."
2. **Deploy (30s)** — show `terraform plan` then `apply` completing. Call out the resource count.
3. **Round trip (45s)** — type `/onboard …` in Slack; show the instant ack, then the result message. Mention the 3-second-ack dispatcher pattern and the async SNS → Slack delivery.
4. **Confidence gate (20s)** — run the ambiguous-role sample (`sample_requests.md`); show it routed to manual review instead of auto-provisioning.
5. **Observability (45s)** — open the CloudWatch dashboard; point to each Lambda firing in sequence (dispatch → onboarding → notify → slack_notifier), the p99 latency, and the alarms/DLQ in OK state.
6. **Quality (15s)** — show `pytest` green.
7. **Close (10s)** — "Least-privilege IAM throughout, secrets in SSM/Secrets Manager, DLQ + alarms on the notification path — built to be operated, not just deployed."

---

## 8. Tear down (after recording — stop charges)

```powershell
cd terraform
terraform destroy
```
*Removes all provisioned resources. The S3 state bucket persists; delete it manually if you're fully done.*

---

### Cost note
With `use_mock_ldap = "true"` there is **no** Simple AD running (the single
biggest cost). What remains — Lambda, API Gateway, SNS, DynamoDB on-demand,
CloudWatch — is pennies for a short demo. Run `terraform destroy` when finished
to be safe.

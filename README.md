# Intelligent Active Directory User Onboarding System

![CI](https://github.com/johnw721/serverless-onboarding/actions/workflows/ci.yml/badge.svg)
![CD](https://github.com/johnw721/serverless-onboarding/actions/workflows/cd.yml/badge.svg)

An automated employee onboarding system that uses Claude (via AWS Bedrock) to process natural language requests and provision Active Directory accounts with appropriate permissions based on role and department.

**Status (v1):** deployed and demoed in mock-LDAP mode (the full Slack → Claude → audit → Slack pipeline, with directory writes logged instead of executed). Real-directory mode is wired up but not yet verified end to end against AWS Managed Microsoft AD.

---

## Demo Video

https://github.com/johnw721/Serverless-Onboarding/blob/main/Screen%20Recording%202026-06-06%20214351.mp4

## Architecture

### Components

| Service | Role |
|---|---|
| **API Gateway (HTTP API)** | Receives `/onboard` and `/offboard` POSTs from Slack slash commands; writes JSON access logs to CloudWatch |
| **AWS Lambda – slack_dispatch_function** | Fronts the `/onboard` and `/offboard` routes; verifies the Slack signing secret, returns a 3s ack, and async-invokes the matching worker (onboarding or offboarding) |
| **AWS Lambda – onboarding_function** | Orchestrates the full onboarding pipeline |
| **AWS Lambda – offboarding_function** | Disables AD account, revokes group membership, logs activity |
| **AWS Lambda – notify_sns_function** | Decoupled SNS publisher, invoked asynchronously |
| **AWS Lambda – slack_notifier_function** | Subscribed to SNS; posts notifications to a Slack incoming webhook (runs outside the VPC for internet egress) |
| **CloudWatch Dashboard** | `ad-lambda-overview` — single-pane invocations, errors, latency, throttles, and DLQ depth |
| **Claude Haiku 4.5 (AWS Bedrock)** | Parses NL requests and writes notifications; assigns AD groups only as a fallback when a department cannot be resolved deterministically (via the US cross-region inference profile) |
| **AWS Managed Microsoft AD (Standard)** | Target directory; users and groups created/disabled over LDAPS |
| **Microsoft Entra ID (optional)** | Synced via Graph API after AD provisioning/offboarding |
| **SSM Parameter Store** | Stores the confidence threshold; adjustable without redeployment |
| **Secrets Manager** | Stores LDAP and Azure credentials securely |
| **DynamoDB** | Append-only audit log of every onboarding/offboarding event with full context |
| **SNS** | Delivers notifications to IT team and hiring managers; encrypted with a customer-managed **KMS** key |
| **SQS** | Dead-letter queue for failed async `notify_sns_function` invocations |
| **Terraform** | Provisions all infrastructure as code |

### Data Flow

```
Slack slash command (/onboard …)
    → API Gateway (POST /onboard, no authorizer)
    → slack_dispatch_function (Lambda)
        ├─ Slack signature invalid → 401 Unauthorized
        └─ Slack signature valid:
                            → async-invokes onboarding_function (InvocationType=Event)
                            → returns "Working on it…" ack to Slack within 3s
                         onboarding_function (Lambda, async)
                            → Bedrock / Claude: parse NL text → structured employee fields
                            → Map department to AD group (deterministic), or Claude if the department is unrecognized; + confidence score
                                ├─ confidence < 80% → DynamoDB ("Pending Review")
                                │                    → SNS (manual review alert)
                                └─ confidence ≥ 80% → LDAP: create user + assign groups
                                                     → DynamoDB ("Success" / "Failed")
                                                     → notify_sns_function (async invoke)
                                                         → Bedrock / Claude: write rich notification
                                                         → SNS: deliver to IT team
                            → SNS → slack_notifier_function → Slack incoming webhook
                                    (result posted back into the channel)

Slack slash command (/offboard …)
    → API Gateway (POST /offboard, no authorizer)
    → slack_dispatch_function (Lambda)
        ├─ Slack signature invalid → 401 Unauthorized
        └─ Slack signature valid → async-invokes offboarding_function; acks Slack in 3s
                       offboarding_function (Lambda, async)
                           → Bedrock / Claude: extract username + identity confidence from NL request
                               ├─ confidence < 95% → DynamoDB ("Offboard Pending Review")
                               │                    → SNS (manual review alert)
                               │                    → 202 response
                               └─ confidence ≥ 95% → LDAP: disable account + remove from all groups
                                                    → Entra ID: disable account (if AZURE_SYNC_ENABLED)
                                                    → DynamoDB ("Offboarded")
                                                    → notify_sns_function (async) → SNS
                                                    → 200 / 400 / 500 response
```

---

## Technical Design Decisions

### Why Claude directly instead of LangChain?

LangChain was considered but ruled out. The three Claude calls in this project — NL parsing, group assignment with confidence scoring, and notification generation — are discrete, well-scoped prompts with structured JSON outputs. Wrapping them in a LangChain agent would add abstraction without value, make debugging harder, and introduce an extra dependency. Direct Bedrock calls via boto3 give full visibility into every prompt and response, which matters for a system making real directory changes.

### Why two Lambda functions?

The SNS notification is decoupled into its own Lambda (`notify_sns_function`) invoked asynchronously (`InvocationType=Event`). This means a transient SNS failure never blocks or retries the main onboarding flow, and each function has a single, testable responsibility.

The SNS client in `notify_sns_function` is initialized at module level rather than inside the handler, so warm Lambda invocations reuse the existing client and skip the boto3 initialization overhead entirely.

### Slack 3-second ack and the dispatcher

Slack slash commands require an HTTP response within 3 seconds, but the onboarding pipeline (Bedrock parsing plus provisioning) can take longer. Rather than refactor `onboarding_function` and its test suite, a thin `slack_dispatch_function` fronts the `/onboard` route: it async-invokes the worker (`InvocationType=Event`) and immediately returns a "Working on it…" acknowledgement. The actual result is delivered back to the channel asynchronously by `slack_notifier_function`. This keeps the worker's logic and tests untouched while giving Slack users instant feedback.

### Slack delivery and VPC egress

`onboarding_function`, `offboarding_function`, and `notify_sns_function` run inside a private VPC with no public egress (they reach AWS services through VPC interface endpoints only). Slack's incoming webhook lives on the public internet, so `slack_notifier_function` runs **outside** the VPC and subscribes to the SNS topic. SNS decouples the in-VPC publishers from the internet-facing delivery function. The webhook URL is stored as an SSM `SecureString` (decrypted at runtime), never as a plaintext environment variable or in Terraform state.

### Why AWS Managed Microsoft AD?

The directory started as AWS Simple AD (about $36/month) and moved to AWS Managed Microsoft AD, Standard edition (roughly $88/month for the two domain controllers AWS runs across two AZs; check current pricing for your region). The reasons:

- **LDAPS.** The Lambdas bind with `use_ssl=True`. Simple AD doesn't support LDAPS; Managed Microsoft AD does once a certificate is configured.
- **Real Active Directory behavior.** Managed Microsoft AD is actual Windows Server AD, so `userAccountControl`, group membership and password policy behave the way they would in a customer's environment instead of through Samba 4 emulation.
- **Still fully managed.** AWS handles patching, backups and multi-AZ availability; there's no Windows Server to maintain.

Two operational notes:

- The directory is only created when `use_mock_ldap` is not `"true"`. Mock mode (the default) skips it entirely, so a demo stack costs pennies and applies in minutes.
- When it is created, it takes about 20–45 minutes (Terraform's create timeout is raised to 90 minutes). The Lambdas take the domain name from a Terraform `local` rather than from the directory resource, so they don't wait on it.
- The directory has `lifecycle { prevent_destroy = true }` because recreating it costs another half hour. Remove that line before an intentional `terraform destroy` or before switching a deployed stack back to mock mode.

The mock LDAP layer (`USE_MOCK_LDAP=true`) stays in place for CI and demos, so the full Claude pipeline runs without touching a directory.

### Confidence thresholds (onboarding and offboarding)

Both flows ask Claude to score its own certainty before taking any action. Requests below the relevant threshold are logged as "Pending Review" and routed to an IT admin SNS alert instead of being auto-processed.

The two thresholds are stored independently in SSM Parameter Store and cached for 5 minutes, so ops can tune them without a Lambda redeployment:

| SSM parameter | Default | Used by |
|---|---|---|
| `/ad-lambda/confidence-threshold` | `0.8` | Onboarding — ambiguous job titles |
| `/ad-lambda/offboard-confidence-threshold` | `0.95` | Offboarding — ambiguous identity |

The offboard threshold is deliberately higher. Disabling an AD account and stripping group memberships is destructive and not trivially undoable, so only high-confidence identity extractions proceed automatically. "Offboard John" returns a 202; "Offboard John Smith" or "Offboard jsmith" goes straight through.

To adjust either threshold without redeploying:

```bash
# Lower the onboarding bar slightly (accept more novel job titles)
aws ssm put-parameter \
  --name "/ad-lambda/confidence-threshold" \
  --value "0.75" \
  --type String \
  --overwrite

# Raise the offboard bar to maximum (always require human sign-off)
aws ssm put-parameter \
  --name "/ad-lambda/offboard-confidence-threshold" \
  --value "1.0" \
  --type String \
  --overwrite
```

The next Lambda invocation after the 5-minute TTL expires will pick up the new value.

### Audit log schema (DynamoDB)

Every event — onboarding, offboarding, pending review, or failure — writes one immutable record. The partition key is a UUID so that multiple events for the same employee are all preserved; using the username as a key caused later writes to silently overwrite earlier ones.

| Attribute | Type | Notes |
|---|---|---|
| `request_id` | String (UUID) | Partition key — unique per event |
| `username` | String | AD username (e.g. `jdoe`) |
| `employee_name` | String | Full name |
| `role` | String | Job title as extracted by Claude |
| `department` | String | Department as extracted or inferred by Claude |
| `groups` | List | AD groups assigned (onboarding) or removed (offboarding) |
| `confidence` | String | Claude's confidence score for the action (stored as decimal string) |
| `timestamp` | Number | Unix epoch of the event |
| `status` | String | `Success`, `Partial`, `Failed`, `Pending Review`, `Offboarded`, `Offboard Pending Review`, `Offboard Failed` |
| `ttl` | Number | Unix epoch; DynamoDB auto-deletes records after 1 year |

`groups` and `confidence` are omitted from records where they are not meaningful (e.g. a parse failure before Claude reached the group-assignment step).

---

## Repository Structure

```
/
├── .github/workflows/
│   ├── ci.yml                 # pytest on every push and PR
│   └── cd.yml                 # tests → terraform plan (PR comment) / apply (push to main)
├── lambda-package/
│   ├── slack_dispatch.py      # Verifies the Slack signature, 3s ack, async-invokes the right worker
│   ├── Lambda_func.py         # Onboarding worker
│   ├── Offboard_func.py       # Offboarding worker
│   ├── bedrock_agent.py       # Claude integration: parsing, group assignment, notifications
│   ├── helpers.py             # Validation, DN sanitizing, department → group map, DynamoDB logging
│   ├── azure_sync.py          # Entra ID sync via Microsoft Graph API (optional)
│   ├── Notify_SNS.py          # Async SNS publisher
│   ├── slack_notifier.py      # SNS → Slack incoming webhook (runs outside the VPC)
│   └── requirements.txt       # boto3, ldap3
├── terraform/
│   ├── Infrastructure.tf      # VPC + endpoints, IAM, Lambdas, API Gateway, SNS + KMS, DynamoDB, SQS DLQ + alarm, SSM, Managed Microsoft AD
│   ├── slack_dispatch.tf      # Dispatcher Lambda, least-privilege role, route permissions
│   ├── slack_notify.tf        # Slack notifier Lambda, webhook SecureString, SNS subscription
│   ├── dashboard.tf           # CloudWatch dashboard (ad-lambda-overview)
│   ├── alarms.tf              # Alarms: onboarding errors, p99 latency
│   ├── variables.tf           # Region, runtime, secrets, mock flag, confidence thresholds
│   └── provider.tf            # AWS provider + S3 backend (bucket and region passed at init)
├── tests/                     # 65 tests
│   ├── test_helpers.py        # Validation and DN sanitizing
│   ├── test_bedrock_agent.py  # Claude functions (Bedrock mocked)
│   ├── test_lambda_handler.py # Onboarding handler paths
│   ├── test_offboard_handler.py # Offboarding handler paths
│   ├── test_azure_sync.py     # Graph API sync (mocked)
│   └── test_e2e_moto.py       # End to end against moto-mocked AWS
├── docs/
│   ├── DEMO_GUIDE.md          # Full runbook: setup → Slack → demo → troubleshooting → teardown
│   ├── sample_requests.md     # Signed sample requests (happy path, manual review, bad input, injection)
│   ├── LEARNING_LESSONS.md    # Build notes and lessons learned
│   ├── runbooks/DLQ-Runbook.md
│   └── incidents/INC-2026-001-DLQ-FIRED.md
├── demo.ps1                   # One-shot demo driver: plan → apply → signed request → dashboard URL
├── LICENSE
└── README.md
```

---

## Setup & Deployment

The full walkthrough, including Slack app setup and troubleshooting, is in [docs/DEMO_GUIDE.md](docs/DEMO_GUIDE.md). The short version:

### Prerequisites

- AWS account with Bedrock access to Claude Haiku 4.5 in `us-west-2` (Anthropic models need a one-time AWS Marketplace subscription; see DEMO_GUIDE "Enable the model")
- Terraform ≥ 1.5 and the AWS CLI
- Python 3.11 + pip
- A Slack app with an Incoming Webhook and two slash commands, `/onboard` and `/offboard`
- An S3 bucket for Terraform state (this project's backend expects it in `us-east-1`)

### 1. Install Lambda dependencies (required before the first apply)

```powershell
# Run from the project root (not terraform/), on one line or with backticks:
pip install -r lambda-package/requirements.txt -t lambda-package/ `
  --platform manylinux2014_x86_64 --python-version 3.11 --only-binary=:all: --upgrade
```

On macOS/Linux, use `\` instead of the backtick for line continuation. `--python-version 3.11` makes pip fetch packages for the Lambda runtime even if your local Python is a different version. Terraform's `pip_install` step repeats this on later applies, but `archive_file` is read at plan time, so on a clean checkout the first zip would otherwise be missing dependencies.

### 2. Create `terraform/secrets.auto.tfvars`

The file is auto-loaded by Terraform and git-ignored. Never commit it.

```hcl
slack_webhook_url        = "https://hooks.slack.com/services/T000/B000/xxxxxxxx"
slack_signing_secret     = "<Slack app → Basic Information → Signing Secret>"
directory_admin_password = "<strong password; only needed when use_mock_ldap = \"false\">"
use_mock_ldap            = "true"
```

### 3. Deploy

```bash
cd terraform
terraform init -backend-config="bucket=<your-tfstate-bucket>" -backend-config="region=us-east-1"
terraform apply
```

In mock mode the apply takes a few minutes. With a real directory, the first apply takes 20–45 minutes. Outputs include `api_endpoint`, `dashboard_url`, and (real directory only) `ad_dns_ip_addresses`.

### 4. Point Slack at the API

Set the slash-command Request URLs to `<api_endpoint>/onboard` and `<api_endpoint>/offboard`, with no query string. Both routes are authenticated by the Slack signature.

### 5. Real directory only: populate the LDAP secrets

Skip this in mock mode. With `use_mock_ldap = "false"`:

```bash
aws secretsmanager put-secret-value --secret-id ldap_server_address --secret-string "<one of the ad_dns_ip_addresses>"
aws secretsmanager put-secret-value --secret-id ldap_username       --secret-string "svc-onboarding@business.abc.com"
aws secretsmanager put-secret-value --secret-id ldap_password       --secret-string "<service account password>"
```

Use a domain controller IP, not `business.abc.com`: the VPC uses AmazonProvidedDNS, which can't resolve the directory's domain.

### Mock LDAP mode

With `use_mock_ldap = "true"` (the default in `cd.yml` and in the example above), every provisioning action that would have been taken is logged to CloudWatch instead of executed against LDAP. Claude parsing, confidence gating, DynamoDB audit records and Slack notifications all run for real. This is how v1 is demoed.

### Responses

The HTTP response comes from `slack_dispatch_function` and only says whether the request was accepted:

- `200` — signature valid; the worker was started and Slack shows "Working on it…"
- `401` — missing or invalid Slack signature, or a timestamp more than 5 minutes old
- `500` — the dispatcher couldn't invoke the worker

The worker's outcome arrives in Slack a few seconds later through SNS: provisioned, sent to manual review (confidence below the threshold or department not stated), or failed (LDAP or AWS error). Each of those is also written to DynamoDB. Requests that fail validation (missing fields, characters not allowed in an LDAP DN) are rejected before any directory write, but today that rejection is silent after the ack: no Slack message and no audit record.

### Tear down

In mock mode, just run `terraform destroy`. If a real directory is deployed, it has `prevent_destroy = true`: delete that line from `aws_directory_service_directory.ad_directory` in `terraform/Infrastructure.tf` first. The S3 state bucket is not managed by Terraform and stays.

---

## Microsoft Entra ID Sync (optional)

After successful AD provisioning (or offboarding), `azure_sync.py` calls the Microsoft Graph API to create or disable the corresponding user in Entra ID — giving the employee Microsoft 365 access automatically.

To enable it, set `azure_sync_enabled = "true"` in your `.tfvars`, then populate the three secrets after `terraform apply`:

```bash
aws secretsmanager put-secret-value --secret-id azure_tenant_id     --secret-string "<your-tenant-id>"
aws secretsmanager put-secret-value --secret-id azure_client_id     --secret-string "<your-client-id>"
aws secretsmanager put-secret-value --secret-id azure_client_secret --secret-string "<your-client-secret>"
```

The app registration in Entra ID needs the `User.ReadWrite.All` application permission (not delegated). Azure sync failures are non-fatal — the AD account is still created and the failure is logged to CloudWatch.

## Offboarding

`/offboard` works exactly like `/onboard`: a Slack slash command fronted by `slack_dispatch_function`, authenticated by **Slack signing-secret HMAC**, with a 3-second ack and the work done asynchronously. In the Slack app set the **Request URL** to `<api_endpoint>/offboard` (no query string). To call it directly, send a valid `X-Slack-Signature` / `X-Slack-Request-Timestamp` — see `docs/DEMO_GUIDE.md` section 4, Option C, with `command=/offboard`.

Claude extracts the username from the natural language request. The offboarding Lambda then disables the AD account (`userAccountControl=514`), removes the user from every group, optionally deprovisions in Entra ID, and logs the event to DynamoDB with status `Offboarded`.

---

## CI/CD

Two GitHub Actions workflows ship with the project.

**`ci.yml`** runs on every push and every PR. It installs Python 3.11 with `boto3`, `ldap3`, `pytest` and `moto`, then runs the full suite: 65 tests, including end-to-end tests against moto-mocked AWS. Bedrock calls are mocked, so CI needs no AWS credentials.

**`cd.yml`** runs the same tests first. On a PR to `main` it posts a `terraform plan` as a PR comment; on a push to `main` it runs `terraform apply`. State lives in the S3 backend configured in `terraform/provider.tf` (bucket passed at `init`, region `us-east-1`). It needs these repository secrets under **Settings → Secrets and variables → Actions**:

| Secret | Description |
|---|---|
| `AWS_ACCESS_KEY_ID` | IAM user access key with permissions to deploy all resources |
| `AWS_SECRET_ACCESS_KEY` | Corresponding secret key |
| `TF_STATE_BUCKET` | S3 bucket for Terraform remote state (in `us-east-1`; create once before the first apply) |
| `DIRECTORY_ADMIN_PASSWORD` | Admin password for AWS Managed Microsoft AD |
| `SLACK_SIGNING_SECRET` | Slack app signing secret; `slack_dispatch_function` verifies every request against it |
| `SLACK_WEBHOOK_URL` | Slack incoming webhook URL used by `slack_notifier_function` |

`cd.yml` sets `TF_VAR_use_mock_ldap: "true"`. Flip it to `"false"` once real LDAP credentials are in Secrets Manager.

---

## Security

- **Secrets Manager and SSM SecureString** — LDAP, Entra ID and directory admin credentials live in Secrets Manager, and the Slack webhook URL is an SSM SecureString. None appear in environment variables or code.
- **Slack request signing** — both routes verify Slack's signing-secret HMAC (`X-Slack-Signature` over the timestamp and raw body, 5-minute replay window) inside `slack_dispatch_function` before any work starts. There's no shared API key and nothing secret in the request URL.
- **VPC isolation** — the in-VPC Lambdas reach AWS services privately: interface endpoints for Secrets Manager, SNS, Lambda, Bedrock Runtime and SSM, placed in both subnets/AZs, plus a gateway endpoint for DynamoDB. Traffic never leaves the AWS backbone. (Gateway-endpoint traffic targets the service's public prefix list, so the Lambda security group must allow egress to that prefix list, not just the VPC CIDR.)
- **Encrypted notifications** — the SNS topic uses a customer-managed KMS key with rotation enabled. The key policy lets CloudWatch alarms publish to the encrypted topic, and the Lambda role gets only `kms:GenerateDataKey` and `kms:Decrypt` on that key.
- **Least-privilege IAM** — the Lambda role is scoped to specific resource ARNs throughout: Secrets Manager, DynamoDB, SNS, KMS, SSM, the notify Lambda function, and the Claude Haiku 4.5 inference profile + foundation-model ARN in Bedrock
- **API access logs** — API Gateway writes JSON access logs (request ID, route, status, integration status, source IP, user agent) to CloudWatch with 14-day retention
- **API Gateway throttling** — burst limit of 10 req/s and sustained rate of 5 req/s protect downstream Bedrock and LDAP from runaway callers, including validly signed ones
- **Confidence gating** — ambiguous requests are flagged for human review rather than auto-provisioned
- **Audit trail** — every event (success, failure, pending review, offboarding) writes an immutable DynamoDB record with a UUID partition key; records include role, department, groups assigned/removed, and Claude's confidence score for full post-incident traceability
- **DLQ + CloudWatch alarm on notification Lambda** — failed async invocations of `notify_sns_function` (after Lambda's built-in retries) are captured in an SQS dead-letter queue by the Lambda service; a CloudWatch alarm fires within 60 seconds if any message lands there, alerting the IT SNS topic automatically

---

## Business Value

| Metric | Before | After |
|---|---|---|
| Time per onboarding | 45–60 min | ~5 min |
| Labor cost per onboarding | ~$50 | ~$2 |
| Human error rate | Variable | Near-zero for known roles |
| Audit completeness | Manual/inconsistent | Automatic, 100% |

Assuming 20 onboardings/month: **~$900/month saved in IT labor**, with a payback period of 3–4 months on the initial build.

---

## Potential Extensions

- **Approval workflow** — Step Functions state machine for manager sign-off before provisioning
- **Self-service portal** — React frontend for HR to submit and track requests
- **Access pattern learning** — Fine-tune group recommendations based on historical provisioning data

---

## Success Criteria Demonstrated

- ✅ **Serverless architecture** — event-driven, scales to zero, pay-per-use
- ✅ **AI integration** — Claude via Bedrock for NL understanding, not just rules matching
- ✅ **Human-in-the-loop design** — dual confidence gating (0.8 onboarding / 0.95 offboarding) prevents silent misprovisioning on both flows
- ✅ **Identity management** — LDAP-based Active Directory automation with exponential-backoff retry
- ✅ **Offboarding** — full mirror flow: account disable, group removal, Entra ID deprovision, audit log
- ✅ **Microsoft Entra ID sync** — Graph API provisioning and deprovisioning for Microsoft 365 access
- ✅ **Security best practices** — Secrets Manager, VPC isolation, least-privilege IAM (resource-specific ARNs throughout), LDAP injection prevention, cryptographically random temp passwords, API Gateway throttling
- ✅ **Resilient notification path** — `notify_sns_function` invoked asynchronously; SQS DLQ captures failures after Lambda's built-in retries; CloudWatch alarm fires within 60 s

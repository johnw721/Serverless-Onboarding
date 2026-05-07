# Intelligent Active Directory User Onboarding System

![CI](https://github.com/<your-org>/<your-repo>/actions/workflows/ci.yml/badge.svg)
![CD](https://github.com/<your-org>/<your-repo>/actions/workflows/cd.yml/badge.svg)

An automated employee onboarding system that uses Claude (via AWS Bedrock) to process natural language requests and provision Active Directory accounts with appropriate permissions based on role and department.

---

## Architecture

### Components

| Service | Role |
|---|---|
| **API Gateway (HTTP API)** | Receives onboarding POST requests from Slack/Teams |
| **AWS Lambda – authorizer_function** | REQUEST authorizer; validates `x-api-key` header on every request |
| **AWS Lambda – onboarding_function** | Orchestrates the full onboarding pipeline |
| **AWS Lambda – offboarding_function** | Disables AD account, revokes group membership, logs activity |
| **AWS Lambda – notify_sns_function** | Decoupled SNS publisher, invoked asynchronously |
| **Claude 3 Haiku (AWS Bedrock)** | Parses NL requests, assigns AD groups, writes notifications |
| **AWS Managed Microsoft AD** | Target directory; users and groups created/disabled via LDAP |
| **Microsoft Entra ID (optional)** | Synced via Graph API after AD provisioning/offboarding |
| **SSM Parameter Store** | Stores the confidence threshold; adjustable without redeployment |
| **Secrets Manager** | Stores LDAP and Azure credentials securely |
| **DynamoDB** | Audit log of every onboarding attempt and its outcome |
| **SNS** | Delivers notifications to IT team and hiring managers |
| **Terraform** | Provisions all infrastructure as code |

### Data Flow

```
Slack / API call (x-api-key header required)
    → API Gateway (POST /onboard)
    → authorizer_function (Lambda REQUEST authorizer)
        ├─ key invalid → 403 Forbidden
        └─ key valid   → onboarding_function (Lambda)
                            → Bedrock / Claude: parse NL text → structured employee fields
                            → Bedrock / Claude: map role to AD groups + confidence score
                                ├─ confidence < 80% → DynamoDB ("Pending Review")
                                │                    → SNS (manual review alert)
                                │                    → 202 response
                                └─ confidence ≥ 80% → LDAP: create user + assign groups
                                                     → DynamoDB ("Success" / "Failed")
                                                     → notify_sns_function (async invoke)
                                                         → Bedrock / Claude: write rich notification
                                                         → SNS: deliver to IT team
                                                     → 200 / 500 response

Slack / API call (x-api-key header required)
    → API Gateway (POST /offboard)
    → authorizer_function
        └─ key valid → offboarding_function (Lambda)
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

### Why AWS Managed Microsoft AD over EC2-based AD?

Higher availability (99.9% SLA), automated patching, and no Windows Server maintenance. Trade-off is cost (~$100–150/month), which is acceptable for an enterprise context and negligible in a dev/demo environment with the directory sized as "Small."

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

---

## Repository Structure

```
/
├── .github/
│   └── workflows/
│       ├── ci.yml           # Run pytest on every push and PR
│       └── cd.yml           # Terraform plan on PR; apply on merge to main
├── lambda-package/
│   ├── Lambda_func.py       # Onboarding Lambda handler
│   ├── Offboard_func.py     # Offboarding Lambda handler
│   ├── bedrock_agent.py     # Claude integration: parsing, group assignment, notifications
│   ├── azure_sync.py        # Entra ID sync via Microsoft Graph API (optional)
│   ├── helpers.py           # Validation, DynamoDB logging, shared maps
│   ├── Notify_SNS.py        # SNS Lambda handler (async, decoupled)
│   ├── api_authorizer.py    # API Gateway REQUEST authorizer (x-api-key validation)
│   └── requirements.txt     # ldap3, boto3, requests
├── terraform/
│   ├── Infrastructure.tf    # All AWS resources
│   ├── variables.tf         # Input variables (CIDRs, runtime, secrets, mock flag)
│   └── provider.tf          # AWS provider + commented S3 remote state backend
├── tests/
│   ├── test_helpers.py      # Unit tests for validate_employee_data + sanitize_dn_value
│   └── test_bedrock_agent.py # Unit tests for all three Claude functions (mocked)
├── QA/                      # Sample curl requests and test payloads
├── .gitignore
└── README.md
```

---

## Setup & Deployment

### Prerequisites

- AWS account with Bedrock access and Claude 3 Haiku model enabled in `us-west-2`
- Terraform ≥ 1.5
- Python 3.11 + pip (must be Linux x86\_64 for Lambda-compatible binaries — use Docker if on Mac/Windows ARM)

### 1. Install Lambda dependencies

```bash
pip install -r lambda-package/requirements.txt \
    -t lambda-package/ \
    --platform manylinux2014_x86_64 \
    --only-binary=:all:
```

> The Terraform `null_resource` handles this automatically on Linux. On Mac/Windows, run the command above manually before `terraform apply`.

### 2. Populate Secrets Manager

After `terraform apply`, populate the three LDAP secrets with values from your directory:

```bash
# The AD DNS name is printed as the `ad_dns_name` Terraform output
aws secretsmanager put-secret-value \
    --secret-id ldap_server_address \
    --secret-string "business.abc.com"

aws secretsmanager put-secret-value \
    --secret-id ldap_username \
    --secret-string "svc-onboarding@business.abc.com"

aws secretsmanager put-secret-value \
    --secret-id ldap_password \
    --secret-string "YourServiceAccountPassword"
```

### 3. Deploy infrastructure

```bash
cd terraform
terraform init
terraform apply
```

The `api_endpoint` output is the URL to send onboarding requests to.

### 4. (Optional) Enable mock LDAP

Set `USE_MOCK_LDAP=true` on the Lambda (or `use_mock_ldap = "true"` in your `.tfvars`) to run the full Claude pipeline without a real Active Directory. Every provisioning action that would have been taken is logged to CloudWatch instead of executed against LDAP. This is the recommended mode for demos and for reviewers who want to test the project without paying for AWS Managed Microsoft AD.

The `cd.yml` workflow sets `TF_VAR_use_mock_ldap: "true"` by default — flip it to `"false"` once real LDAP credentials are in Secrets Manager.

### 5. Send an onboarding request

```bash
curl -X POST https://<api_endpoint>/onboard \
  -H "Content-Type: application/json" \
  -H "x-api-key: <your_onboarding_api_key>" \
  -d '{"request": "Please onboard Sarah Chen as a senior data scientist starting Monday"}'
```

**Responses:**
- `200` — user created in AD, IT notified
- `202` — role too ambiguous for auto-provisioning; IT alerted for manual review
- `400` — Claude could not extract required fields from the request
- `403` — missing or invalid API key
- `500` — LDAP or AWS service error

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

Send a `POST /offboard` request with the same `x-api-key` header:

```bash
curl -X POST https://<api_endpoint>/offboard \
  -H "Content-Type: application/json" \
  -H "x-api-key: <your_onboarding_api_key>" \
  -d '{"request": "Please offboard John Doe"}'
```

Claude extracts the username from the natural language request. The offboarding Lambda then disables the AD account (`userAccountControl=514`), removes the user from every group, optionally deprovisions in Entra ID, and logs the event to DynamoDB with status `Offboarded`.

---

## CI/CD

Two GitHub Actions workflows ship with the project.

**`ci.yml`** runs on every push and every PR. It installs Python 3.11 with `boto3`, `ldap3`, and `pytest`, then runs the full test suite. All Bedrock calls are mocked — no AWS credentials are needed for CI.

**`cd.yml`** runs after tests pass. On a PR to `main` it posts a `terraform plan` as a PR comment. On merge to `main` it runs `terraform apply`. It requires the following secrets configured in **Settings → Secrets and variables → Actions**:

| Secret | Description |
|---|---|
| `AWS_ACCESS_KEY_ID` | IAM user access key with permissions to deploy all resources |
| `AWS_SECRET_ACCESS_KEY` | Corresponding secret key |
| `TF_STATE_BUCKET` | S3 bucket name for Terraform remote state (create once before first apply) |
| `DIRECTORY_ADMIN_PASSWORD` | Admin password for the AWS Managed Microsoft AD |
| `ONBOARDING_API_KEY` | API key to protect the onboarding endpoint |

Before enabling the CD workflow, uncomment and configure the S3 backend in `terraform/provider.tf` so Terraform state is shared between runs rather than stored locally on the runner.

Update the badge URLs at the top of this file by replacing `<your-org>/<your-repo>` with your GitHub repository path.

---

## Security

- **Secrets Manager** — LDAP credentials never appear in environment variables or code
- **VPC isolation** — both Lambda functions run inside the VPC; all AWS service calls go through Interface Endpoints and never leave the AWS backbone
- **Least-privilege IAM** — Lambda role is scoped to the specific actions it needs; Secrets Manager and DynamoDB access is resource-specific, while Bedrock and Lambda invoke use wildcards scoped to the account
- **API key authentication** — all requests authenticated via a Lambda REQUEST authorizer before reaching the onboarding pipeline
- **Confidence gating** — ambiguous requests are flagged for human review rather than auto-provisioned
- **Audit trail** — every onboarding attempt (success, failure, or pending review) is written to DynamoDB with a timestamp

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
- **Azure AD sync** — Extend provisioning to Entra ID for Microsoft 365 access
- **Offboarding** — Mirror flow that disables accounts, revokes group membership, and logs activity
- **Self-service portal** — React frontend for HR to submit and track requests
- **Access pattern learning** — Fine-tune group recommendations based on historical provisioning data

---

## Success Criteria Demonstrated

- ✅ **Serverless architecture** — event-driven, scales to zero, pay-per-use
- ✅ **AI integration** — Claude via Bedrock for NL understanding, not just rules matching
- ✅ **Human-in-the-loop design** — confidence gating prevents silent misprovisioning
- ✅ **Identity management** — LDAP-based Active Directory automation
- ✅ **Security best practices** — Secrets Manager, VPC isolation, least-privilege IAM
- ✅ **Infrastructure as code** — fully reproducible Terraform deployment
- ✅ **Observability** — CloudWatch Logs + DynamoDB audit trail

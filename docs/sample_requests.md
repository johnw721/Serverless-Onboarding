# Sample API Requests

Both routes (`/onboard` and `/offboard`) only accept requests signed the way
Slack signs slash commands. The easiest way to send them is from Slack itself.
To call the API directly, define this helper once (bash, needs `openssl`):

```bash
API="<api_endpoint Terraform output>"
SECRET="<your slack_signing_secret>"

slack_post () {   # usage: slack_post /onboard "request text"
  local body="command=$1&text=$(python3 -c 'import sys,urllib.parse;print(urllib.parse.quote(sys.argv[1]))' "$2")"
  local ts; ts=$(date +%s)
  local sig="v0=$(printf 'v0:%s:%s' "$ts" "$body" | openssl dgst -sha256 -hmac "$SECRET" | sed 's/^.* //')"
  curl -s -w '\nHTTP %{http_code}\n' -X POST "$API$1" \
    -H "Content-Type: application/x-www-form-urlencoded" \
    -H "X-Slack-Request-Timestamp: $ts" \
    -H "X-Slack-Signature: $sig" \
    --data "$body"
}
```

For PowerShell, see `DEMO_GUIDE.md` section 4, Option C.

> **Asynchronous response model.** Every signed request gets the same immediate
> reply from `slack_dispatch_function`: HTTP 200 with
> `{"text": "…Working on it - I'll post the result here shortly."}`. The real
> outcome comes from the worker a few seconds later: posted to Slack through
> SNS → `slack_notifier_function`, and written to DynamoDB. The "Outcome" notes
> below describe that second step. Invalid input is the exception: the worker
> rejects it, but on an async invoke its return value is discarded, so today
> nothing posts to Slack and nothing is written to DynamoDB. Only a Claude
> *parse* failure leaves an `[ERROR]` line in `/aws/lambda/onboarding_function`.

Group assignment is driven by the **department**, so state one. Known
departments (Engineering, Data Science, Product, Human Resources, Sales,
Marketing, Support, Finance, IT, Operations, plus common aliases like "HR" or
"eng") map deterministically. A missing or unknown department is sent to review.

---

## Happy path — standard role and department

```bash
slack_post /onboard "Please onboard Sarah Chen as a Data Scientist in the Data Science department, starting Monday."
```

**Outcome:** user `schen` provisioned (logged only, in mock mode) into
`Data Science` + `All Employees`; Slack gets a success message; DynamoDB status
`Success`.

---

## Ambiguous role — triggers manual review

```bash
slack_post /onboard "We need to set up an account for Marcus Webb, he is joining as our new Innovation Catalyst."
```

**Outcome:** confidence below the 0.8 threshold (no department stated), so
nothing is provisioned. Slack gets a manual-review alert; DynamoDB status
`Pending Review`.

---

## Unsigned request — rejected by the dispatcher (expect 401)

```bash
curl -s -w '\nHTTP %{http_code}\n' -X POST "$API/onboard" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  --data "command=/onboard&text=Onboard%20someone"
```

**Response:** `401` with `{"text": "Unauthorized request."}`. The worker never
runs. The same happens for a wrong signature or a timestamp more than 5 minutes
old.

---

## Unparseable request — bad input

```bash
slack_post /onboard "!!@@##"
```

**Outcome:** the worker rejects it before any directory or DynamoDB write,
returning `Missing required employee data fields: …` (or, if Claude can't parse
it at all, logging `Failed to parse onboarding request`). No Slack follow-up
after the ack.

---

## Username injection attempt — rejected before LDAP

```bash
slack_post /onboard "Onboard the user jdoe,CN=Admins as a Software Engineer in Engineering"
```

**Outcome:** rejected by DN sanitizing:
the worker returns `Value 'jdoe,CN=Admins' contains characters that are not
permitted in an LDAP DN...` and stops before LDAP. No Slack follow-up after the
ack, and no log line yet.

---

## Offboarding — clear identity

```bash
slack_post /offboard "Please offboard jsmith, last day was Friday."
```

**Outcome:** identity confidence ≥ 0.95, so the account is disabled and removed
from all groups (logged only, in mock mode); Entra ID disabled if Azure sync is
on; Slack notified; DynamoDB status `Offboarded`.

## Offboarding — ambiguous identity

```bash
slack_post /offboard "Offboard John"
```

**Outcome:** below the 0.95 offboarding threshold, so nothing is disabled.
Slack gets a manual-review alert; DynamoDB status `Offboard Pending Review`.

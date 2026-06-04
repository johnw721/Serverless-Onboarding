# Sample API Requests

> **Note — asynchronous response model.** The `/onboard` route is now fronted by
> `slack_dispatch_function`, which returns an immediate acknowledgement
> (`{"text": "Working on it…"}`, HTTP 200) and processes the request
> asynchronously. The **final** result (success, manual-review, or validation
> error) is delivered to Slack via `slack_notifier_function`, not in the
> synchronous HTTP body. The "Expected response" blocks below describe the
> *worker's* outcome — i.e. what you'll see in Slack / CloudWatch Logs — except
> the 403 case, which the authorizer still returns synchronously before dispatch.

Replace `<API_ENDPOINT>` with the `api_endpoint` Terraform output and `<YOUR_API_KEY>` with the value you passed for `var.onboarding_api_key`.

---

## Happy path — standard role (expect 200)

```bash
curl -X POST https://<API_ENDPOINT>/onboard \
  -H "Content-Type: application/json" \
  -H "x-api-key: <YOUR_API_KEY>" \
  -d '{"request": "Please onboard Sarah Chen as a Data Scientist starting Monday."}'
```

**Expected response:**
```json
{"message": "User schen onboarded successfully."}
```

---

## Ambiguous role — triggers manual review (expect 202)

```bash
curl -X POST https://<API_ENDPOINT>/onboard \
  -H "Content-Type: application/json" \
  -H "x-api-key: <YOUR_API_KEY>" \
  -d '{"request": "We need to set up an account for Marcus Webb, he is joining as our new Innovation Catalyst."}'
```

**Expected response:**
```json
{
  "message": "Onboarding request for Marcus Webb received but requires manual review due to an ambiguous role description. The IT team has been notified.",
  "username": "mwebb",
  "confidence": 0.45
}
```

---

## Slack-style URL-encoded body (expect 200)

```bash
curl -X POST https://<API_ENDPOINT>/onboard \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -H "x-api-key: <YOUR_API_KEY>" \
  --data-urlencode "text=Onboard James Rivera as a Finance Analyst in the Finance department"
```

---

## Missing API key — unauthorized (expect 403)

```bash
curl -X POST https://<API_ENDPOINT>/onboard \
  -H "Content-Type: application/json" \
  -d '{"request": "Onboard someone"}'
```

**Expected response:** `403 Forbidden`

---

## Unparseable request — bad input (expect 400)

```bash
curl -X POST https://<API_ENDPOINT>/onboard \
  -H "Content-Type: application/json" \
  -H "x-api-key: <YOUR_API_KEY>" \
  -d '{"request": "!!@@##"}'
```

**Expected response:**
```json
{"message": "Missing required employee data fields: username, name, Role, Department"}
```

---

## Username injection attempt — rejected before LDAP (expect 400)

```bash
curl -X POST https://<API_ENDPOINT>/onboard \
  -H "Content-Type: application/json" \
  -H "x-api-key: <YOUR_API_KEY>" \
  -d '{"request": "Onboard the user jdoe,CN=Admins as a Software Engineer"}'
```

**Expected response:**
```json
{"message": "Value 'jdoe,CN=Admins' contains characters that are not permitted in an LDAP DN..."}
```

# INC-2026-002 — Investigation commands, explained

Companion to [INC-2026-002-SIMPLE-AD-CREATE-BLOCKED.md](INC-2026-002-SIMPLE-AD-CREATE-BLOCKED.md).
Every command used to reconstruct the incident, in the order they were run, with
what each part does and what the result told us. Commands are PowerShell; the
`aws` parts are identical in bash.

---

## 0. Point the CLI at the right region

```powershell
aws configure            # answered: Default region name [us-east-1]: us-west-2
```

| Part | Meaning |
|---|---|
| `aws configure` | Interactive prompt that saves credentials, default region and output format to `~/.aws/config` |
| region `us-west-2` | Where this stack is deployed. Every command below queries only the default region unless you pass `--region` |

**Why it mattered:** the first CloudTrail query returned `"Events": []` because the
CLI was still on `us-east-1`. An empty result from the wrong region looks exactly
like "nothing happened". For a one-off override use `--region us-west-2`, or for
one PowerShell session `$env:AWS_DEFAULT_REGION = "us-west-2"`.

---

## 1. Was the KMS key alive on the incident date?

```powershell
aws cloudtrail lookup-events `
  --lookup-attributes AttributeKey=EventName,AttributeValue=CreateKey `
  --query "Events[].[EventTime,Username]" --output table
```

| Part | Meaning |
|---|---|
| `cloudtrail lookup-events` | Searches CloudTrail **event history**: the free, built-in record of management API calls for the last **90 days** in the current region |
| `--lookup-attributes AttributeKey=EventName,AttributeValue=CreateKey` | Filter to one API call by name. Only **one** attribute is allowed per query (EventName, EventSource, Username, ResourceName…) |
| `--query "Events[].[EventTime,Username]"` | A JMESPath expression run on the JSON response: for every event, keep only the time and who made the call |
| `--output table` | Print as a table instead of JSON |
| `` ` `` (backtick) | PowerShell line continuation (bash uses `\`) |

Repeated with `AttributeValue=ScheduleKeyDeletion`. Without `--query`, the output
includes the full raw `CloudTrailEvent` JSON for every event, which is why the
earlier unfiltered runs had to be stopped with Ctrl+C.

**Result:** key created 2026-09-23 15:42 EDT, deletion scheduled 16:21 EDT. The
`DeleteKey` seen on 09-30 was made by `AWS Internal`: AWS finishing the 7-day
deletion window, not a person. So the key didn't exist on 09-02, and KMS was ruled out for INC-2026-001.

---

## 2. Is the stack deployed right now?

```powershell
aws lambda list-functions --query "Functions[].FunctionName"
```

Lists every Lambda function in the region and keeps only the names.
**Result:** `[]`. Nothing deployed, so all remaining evidence had to come from history, not live resources.

---

## 3. Did the notify Lambda log anything that week?

```powershell
aws logs filter-log-events --log-group-name /aws/lambda/notify_sns_function `
  --start-time 1788220800000 --end-time 1788825600000 `
  --query "events[].[timestamp,message]" --output text
```

| Part | Meaning |
|---|---|
| `logs filter-log-events` | Searches one CloudWatch Logs log group across all of its streams |
| `--log-group-name /aws/lambda/notify_sns_function` | Lambda logs live in `/aws/lambda/<function name>`. Terraform doesn't manage this group, so it **survives `terraform destroy`**, and with no retention set it never expires |
| `--start-time` / `--end-time` | **Epoch milliseconds**, not dates. 1788220800000 = 2026-09-01 00:00 UTC, 1788825600000 = 2026-09-08 |
| `--filter-pattern "?ERROR ?AccessDenied"` (optional) | `?` means OR: match lines containing either term |

Convert a date to epoch ms in PowerShell:
`[DateTimeOffset]::new(2026,9,1,0,0,0,[TimeSpan]::Zero).ToUnixTimeMilliseconds()`

**Result:** no output. The function didn't run at all that week.

---

## 4. Did the DLQ ever receive a message? Did the notify Lambda ever fail?

```powershell
aws cloudwatch get-metric-statistics --namespace AWS/SQS --metric-name NumberOfMessagesSent `
  --dimensions Name=QueueName,Value=notify-sns-dlq `
  --start-time 2026-05-01T00:00:00Z --end-time 2026-10-01T00:00:00Z `
  --period 86400 --statistics Sum --output table
```

| Part | Meaning |
|---|---|
| `--namespace AWS/SQS --metric-name NumberOfMessagesSent` | Which metric: messages written into a queue. For the Lambda check, `AWS/Lambda` + `Errors` |
| `--dimensions Name=QueueName,Value=notify-sns-dlq` | Which queue (for Lambda: `Name=FunctionName,Value=notify_sns_function`) |
| `--start-time` / `--end-time` | ISO-8601 timestamps here; the `Z` means UTC |
| `--period 86400` | One data point per day (86,400 s). Metrics are kept **15 months**, but data older than 63 days only exists at 1-hour resolution or coarser, so the period must be a multiple of 3600 |
| `--statistics Sum` | Add up all values within each period |

**How to read it:** a row with `0.0` means the resource existed that day and the
value was zero. A missing day means there's no data at all (the resource didn't
exist or never reported). An empty table means no data in the whole range.

**Result:** every row is `0.0` (June and September). The DLQ never received a
message and the notify Lambda never errored. INC-2026-001 is unconfirmed.

---

## 5. What happened to the directory? (the key query)

```powershell
(aws cloudtrail lookup-events --lookup-attributes AttributeKey=EventSource,AttributeValue=ds.amazonaws.com `
  --start-time 2026-09-20 --output json | ConvertFrom-Json).Events | ForEach-Object {
    $e = $_.CloudTrailEvent | ConvertFrom-Json
    "{0}  {1}  {2}  {3}" -f $e.eventTime, $e.eventName, $e.errorCode, $e.errorMessage
}
```

Read it from the inside out:

| Piece | What it does |
|---|---|
| `AttributeKey=EventSource,AttributeValue=ds.amazonaws.com` | Every call to **AWS Directory Service** (the `ds` API): creates, deletes and describes |
| `--start-time 2026-09-20` | Only events from that date onward (a date alone means midnight UTC) |
| `--output json \| ConvertFrom-Json` | Turn the CLI's JSON into PowerShell objects |
| `( … ).Events` | Take the list of events from the response |
| `ForEach-Object { … }` | Run the block once per event; `$_` is the current event |
| `$_.CloudTrailEvent \| ConvertFrom-Json` | The interesting fields (`errorCode`, `errorMessage`) are inside `CloudTrailEvent`, which is a JSON **string** nested inside the JSON, so it has to be parsed a second time |
| `"{0}  {1}  {2}  {3}" -f …` | .NET format string: one line per event with time, API call, error code, error message |

`--query` can't do this on its own because it can't parse the nested JSON string.
That's why PowerShell does the second step.

**How to read the output:**
- `CreateDirectory … ClientException Simple AD is no longer open to new customers` — the root cause, three times
- `CreateMicrosoftAD` — the fix being applied
- Runs of `DescribeDirectories` every ~10 s — Terraform polling until the directory is active (the length of the run = creation time, ~31 and ~33 min)
- `DeleteDirectory` — a `terraform destroy`

---

## 6. Still to run

```powershell
# Did CD's apply (pushes on 2026-09-18) also hit the rejection?
(aws cloudtrail lookup-events --lookup-attributes AttributeKey=EventName,AttributeValue=CreateDirectory `
  --start-time 2026-09-17 --output json | ConvertFrom-Json).Events | ForEach-Object {
    $e = $_.CloudTrailEvent | ConvertFrom-Json
    "{0}  {1}  {2}  {3}" -f $e.eventTime, $e.userIdentity.arn, $e.errorCode, $e.sourceIPAddress
}

# Save the evidence before CloudTrail drops it (90 days)
aws cloudtrail lookup-events --lookup-attributes AttributeKey=EventSource,AttributeValue=ds.amazonaws.com `
  --start-time 2026-09-17 --output json > local-artifacts/INC-2026-002-cloudtrail-ds.json
```

`userIdentity.arn` and `sourceIPAddress` tell you **who** made the call: your own
user from your machine, or the CD workflow's IAM user from a GitHub runner.

---

## Mistakes made along the way (kept on purpose)

| Mistake | Symptom | Lesson |
|---|---|---|
| CLI default region `us-east-1` | `"Events": []` | Check the region before trusting an empty result |
| Literal `<acct>` placeholder left in an ARN | `Invalid namespace: <acct>` | Fill placeholders, or set `$acct = aws sts get-caller-identity --query Account --output text` first |
| Epoch value computed for the wrong year (2025) | Would have searched an empty window | Generate timestamps with a command, not by hand |
| No `--query` on lookups | Huge JSON, had to Ctrl+C | Project only the fields you need |

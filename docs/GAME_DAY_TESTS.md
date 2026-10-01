# Game Day Tests — AD Lambda

Three controlled fault-injection tests. Each one produces the evidence a real
incident would (logs, metrics, alarm history, DLQ contents) on purpose, with a
known start time, so it can be written up as an RCA. Run them in the order below:
test 2 first proves you are testing the code you think you deployed.

Everything is in **us-west-2**. Set it once per PowerShell session so no command
silently queries the wrong region:

```powershell
$env:AWS_DEFAULT_REGION = "us-west-2"
$acct = aws sts get-caller-identity --query Account --output text
mkdir evidence -ErrorAction SilentlyContinue   # git-ignored; move what you keep into local-artifacts/ or the incident record
```

## 0. Preflight (before any test)

| Check | Command | Expect |
|---|---|---|
| Stack is deployed | `aws lambda list-functions --query "Functions[].FunctionName"` | `onboarding_function`, `offboarding_function`, `notify_sns_function`, `slack_dispatch_function`, `slack_notifier_function` |
| Alarms are healthy | `aws cloudwatch describe-alarms --query "MetricAlarms[].[AlarmName,StateValue]" --output table` | All `OK` (or `INSUFFICIENT_DATA` on a fresh stack) |
| DLQ is empty | `aws sqs get-queue-attributes --queue-url (aws sqs get-queue-url --queue-name notify-sns-dlq --query QueueUrl --output text) --attribute-names ApproximateNumberOfMessages` | `0` |
| Topic is KMS-encrypted | `aws sns get-topic-attributes --topic-arn arn:aws:sns:us-west-2:${acct}:notification_topic --query Attributes.KmsMasterKeyId` | A key ARN |
| Happy path works | `./demo.ps1 -SkipApply` | Ack, then a success message in Slack |

Write down the UTC time you start each test. Every timeline entry is measured from it.

Evidence expires: DLQ messages after 14 days, alarm history after 14 days,
CloudTrail event history after 90 days. **Export at the time of the test, not later.**

---

## Test 1 — Notification failure reaches the DLQ and the alarm reaches Slack

Reproduces INC-2026-001 under control.

**Hypothesis.** If `notify_sns_function` fails, Lambda retries it twice, the
event lands in `notify-sns-dlq`, and `notify-sns-dlq-messages` goes to ALARM
and posts to Slack through the KMS-encrypted topic. Expected: DLQ about 3 minutes
after the first failure (async retries), alarm within about 60 seconds after that.

**Inject.** Invoke the function asynchronously with a topic it can't publish to.
No infrastructure change:

```powershell
'{"topic_arn":"arn:aws:sns:us-west-2:' + $acct + ':gameday-does-not-exist","subject":"GD1","message":"game day 1"}' | Out-File -Encoding ascii gd1.json
aws lambda invoke --function-name notify_sns_function --invocation-type Event `
  --cli-binary-format raw-in-base64-out --payload file://gd1.json evidence/gd1-invoke.json
```

**Capture, in this order:**
1. DLQ message *without deleting it*. The attributes include `ErrorCode`, `ErrorMessage` and `RequestID`:
   `aws sqs receive-message --queue-url <dlq-url> --attribute-names All --message-attribute-names All > evidence/gd1-dlq.json`
2. Alarm history: `aws cloudwatch describe-alarm-history --alarm-name notify-sns-dlq-messages > evidence/gd1-alarm-history.json`
3. Logs: `aws logs filter-log-events --log-group-name /aws/lambda/notify_sns_function --start-time <start, epoch ms> > evidence/gd1-logs.json`
4. Screenshot of the alarm message in Slack, and the dashboard's DLQ-depth widget.

**Pass.** 3 invocation errors (1 + 2 retries), 1 DLQ message with error
attributes, alarm ALARM, and the alarm line visible in Slack. Record the actual
times: this measures the "60-second detection" claim instead of assuming it.

**Fail signals.** DLQ fills but no Slack message: the CloudWatch → encrypted-topic
path is broken (KMS key policy). That is the suspected INC-2026-001 root cause.

**Roll back.** `aws sqs purge-queue --queue-url <dlq-url>`. Within a few minutes
the alarm returns to OK and posts an OK message to Slack. Capture that too.

*Optional 1b, the real root cause:* remove the `KMSForEncryptedSNSTopic` statement
from the Lambda policy, `terraform apply`, run a normal `/onboard`, and confirm
the same chain starts from a genuine `KMS AccessDenied`. Restore with `terraform apply`.

---

## Test 2 — A code change actually ships

Regression test for the stale-deploy bug (`archive_file` + `depends_on` froze the zip).

**Hypothesis.** Editing a handler and running `terraform apply` updates every
function that uses the shared zip, and the new code runs on the next invocation.

**Before.** Record the deployed code hashes:
`aws lambda list-functions --query "Functions[].[FunctionName,CodeSha256,LastModified]" --output table > evidence/gd2-before.txt`

**Inject.** Add one log line near the top of `lambda_handler` in `lambda-package/slack_dispatch.py`:
`logger.info("build-marker GD2-<yyyymmdd>")`. Then:

```powershell
cd terraform
terraform plan -out=gd2.tfplan   # expect in-place updates to the Lambda functions, not "No changes"
terraform apply gd2.tfplan
cd ..
```

**After.** Repeat the `list-functions` command into `evidence/gd2-after.txt`, run
`./demo.ps1 -SkipApply`, then look for the marker:
`aws logs filter-log-events --log-group-name /aws/lambda/slack_dispatch_function --filter-pattern '"build-marker"'`

**Pass.** The plan shows updates, `CodeSha256` and `LastModified` changed, and the marker appears in the logs.
**Fail signal.** The plan says `No changes` or the hash didn't change: the bug is back.
**Roll back.** Remove the marker line and apply again. That second apply is a second data point.
Delete `gd2.tfplan` afterwards; plan files contain secrets.

---

## Test 3 — A broken in-VPC dependency fails fast and loudly

Regression test for the silent 60-second timeouts (unbounded in-VPC calls).

**Hypothesis.** If the worker can't reach DynamoDB, it fails within seconds with
an explicit error in its logs, not a silent run to the 60-second timeout.

**Inject.** Remove the security-group egress rule that lets the Lambdas reach the
DynamoDB gateway endpoint (simulated drift):

```powershell
$sg = aws ec2 describe-security-groups --filters Name=group-name,Values=lambda_sg --query "SecurityGroups[0].GroupId" --output text
$pl = aws ec2 describe-managed-prefix-lists --filters Name=prefix-list-name,Values=com.amazonaws.us-west-2.dynamodb --query "PrefixLists[0].PrefixListId" --output text
aws ec2 describe-security-group-rules --filters Name=group-id,Values=$sg --output table > evidence/gd3-sg-before.txt
aws ec2 revoke-security-group-egress --group-id $sg --ip-permissions "IpProtocol=tcp,FromPort=443,ToPort=443,PrefixListIds=[{PrefixListId=$pl}]"
```
(This matches `aws_security_group_rule.lambda_egress_dynamodb`: TCP 443 to the DynamoDB prefix list.)

Then send a valid request: `./demo.ps1 -SkipApply`.

**Capture.** The worker log stream for that request
(`aws logs tail /aws/lambda/onboarding_function --since 10m --format short > evidence/gd3-logs.txt`),
the `REPORT` line's `Duration`, whether Slack got a failure message, and the
`onboarding-function-errors` alarm state.

**Pass.** Duration well under 60 seconds, a clear `[ERROR]` naming DynamoDB, no
`Status: timeout`. Note honestly whether the user was told (Slack) and whether
the alarm fired: a handled error doesn't count as a Lambda `Errors` metric.

**Roll back.** `cd terraform; terraform plan` shows the missing rule to be
re-created (drift detection). Screenshot that, then `terraform apply`.

---

## After each test

Fill in an incident record (`docs/incidents/` template) from the evidence:
timeline from the recorded timestamps, detection = which signal fired first,
time to detect, root cause = the injection, and any **surprises** as findings
and corrective actions. Surprises are the most valuable part of a game day.

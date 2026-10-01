Incident ID:            INC-2026-002-adlambda-simple-ad-create-blocked
Title:                  Deploy blocked: Simple AD closed to new customers
Status:                 Resolved
Author / Date written:  Jarel Wright / 2026-10-01
Environment:            dev (personal AWS account, us-west-2)
Priority:               P3 (deploy blocked; no end-user impact)

## Impact
`terraform apply` could not create the Active Directory directory, so the stack
could not be deployed as committed. Visible immediately (the apply failed).
No data loss and no user impact: the system ran in mock-LDAP mode and was not
serving anyone.

The larger impact was on **reproducibility**: the committed configuration could not
be deployed by anyone in a new AWS account, which is exactly how reviewers would
run a public portfolio repo.

## Timeline (UTC; EDT = UTC−4)
| Time (UTC) | Event | Source |
|---|---|---|
| 2026-09-18 04:44 | Commit `19cc5f8` "updated from simple ad to aws managed ad small instance". The committed directory resource still had no `type` and `size = "Small"`, i.e. Simple AD | git |
| 2026-09-20 08:15:52 | `CreateDirectory` (Simple AD) → `ClientException: Simple AD is no longer open to new customers` | CloudTrail |
| 2026-09-20 08:32:23 | Retry → same error | CloudTrail |
| 2026-09-20 08:36:23 | Retry → same error | CloudTrail |
| 2026-09-20 08:56:19 | `CreateMicrosoftAD` succeeds (config changed to `type = "MicrosoftAD"`, `edition = "Standard"`) | CloudTrail |
| 2026-09-20 08:56 → 09:27:42 | Terraform polls `DescribeDirectories` every ~10 s until the directory is active (~31 min) | CloudTrail |
| 2026-09-22 04:12:14 | `DeleteDirectory` (teardown), complete by 04:14:25 | CloudTrail |
| 2026-09-23 19:42:04–08 | Redeploy: KMS `CreateKey`, then `CreateMicrosoftAD`; active by ~20:14:53 (~33 min) | CloudTrail |
| 2026-09-23 20:21:27–54 | Teardown: `DeleteDirectory`, KMS `ScheduleKeyDeletion` (7-day window) | CloudTrail |
| 2026-09-30 20:22:43 | KMS key deleted by AWS at the end of the window | CloudTrail |

- **Occurred / Detected:** 2026-09-20 08:15:52
- **Mitigated** (working directory create issued): 08:56:19
- **Resolved** (directory active): ~09:27:42
- **Time to detect:** 0 min (synchronous API error during apply)
- **Time to mitigate:** 40 min 27 s
- **Time to resolve:** ~71 min

## Detection
The `terraform apply` run failed on the directory resource with the AWS error
message. No alarm was involved; the failure was synchronous and immediate.

## Key components involved
`aws_directory_service_directory.ad_directory` (Terraform AWS provider), AWS
Directory Service, CloudTrail.

## Assumptions
- The account was treated as a "new customer" for Simple AD. An earlier attempt
  to create a Simple AD directory (2026-05-04, `apply.log`) has no recorded outcome.
- The `DescribeDirectories` calls at 21:02:07 on 2026-09-25 and 2026-09-30 are
  not from Terraform (no apply ran then); their source is unexplained.

## Symptoms
`terraform apply` error on the directory resource:
`ClientException: Simple AD is no longer open to new customers. For capabilities
similar to Simple AD, explore AWS Managed Microsoft AD.` Two retries returned the
same error, which rules out a transient failure.

## Investigation
1. Read the error: an explicit service-availability rejection, not a quota, IAM or networking failure.
2. Retried twice (08:32, 08:36) to rule out a transient error: identical result.
3. Confirmed in AWS docs that Simple AD is closed to new customers; existing
   customers keep full use and alternatives are Managed Microsoft AD or AD Connector.
4. Switched the resource to `type = "MicrosoftAD"`, `edition = "Standard"` and re-applied.
5. Retrospective (2026-10-01): reconstructed the full timeline from CloudTrail
   event history for `ds.amazonaws.com` and `kms.amazonaws.com`. A first query
   returned nothing because the CLI default region was us-east-1, not us-west-2.

## Root cause
The Terraform resource did not set `type`, so the AWS provider defaulted it to
`SimpleAD`. AWS has closed Simple AD to new customers, and `CreateDirectory`
rejected the request from this account.

## Contributing factors
- **Implicit default.** The directory type was never stated in code, so a
  platform-level change to one service type wasn't visible in review.
- **Commit message didn't match the change.** `19cc5f8` described a switch to
  Managed AD, but the committed resource was still Simple AD. The actual switch
  lived only in uncommitted local changes.
- **No check against the real API before merge.** `terraform validate` and `plan`
  can't detect a service-availability rejection; only an actual create does.
- **The directory was mandatory even in mock mode**, so a component the demo
  never uses blocked the whole deploy.

## Resolution
Changed the directory to AWS Managed Microsoft AD (Standard edition) with an
explicit `type`, raised the create timeout to 90 minutes, and added
`prevent_destroy`. The directory then created successfully twice (~31 and ~33 min).

## Corrective actions
| Action | Owner | Status | Due |
|---|---|---|---|
| Set `type = "MicrosoftAD"` explicitly; never rely on the provider default | Jarel Wright | Done (uncommitted) | v1 |
| Skip the directory entirely in mock mode (`count` on `use_mock_ldap`) so an unused dependency can't block deploys | Jarel Wright | Done (uncommitted) | v1 |
| Update README / DEMO_GUIDE rationale and cost notes for Managed AD | Jarel Wright | Done (uncommitted) | v1 |
| Commit the fix with a message that matches the diff and references this incident | Jarel Wright | Open | v1 |
| Check whether CD's apply (pushes on 2026-09-18) also hit this: CloudTrail `CreateDirectory` from 2026-09-17 | Jarel Wright | Open | v1 |
| Real-directory readiness: `BASE_DN` at domain root isn't writable on Managed AD; set the delegated OU and enable LDAPS | Jarel Wright | Open | post-v1 |
| Periodic fresh-account deploy test (or sandbox apply) to catch platform-availability changes | Jarel Wright | Open | post-v1 |

## Evidence
Every investigation command, explained: [INC-2026-002-CLI-COMMANDS.md](INC-2026-002-CLI-COMMANDS.md)

CloudTrail query used:
```powershell
(aws cloudtrail lookup-events --region us-west-2 --lookup-attributes AttributeKey=EventSource,AttributeValue=ds.amazonaws.com `
  --start-time 2026-09-20 --output json | ConvertFrom-Json).Events | ForEach-Object {
    $e = $_.CloudTrailEvent | ConvertFrom-Json
    "{0}  {1}  {2}  {3}" -f $e.eventTime, $e.eventName, $e.errorCode, $e.errorMessage }
```
Rejected request IDs: `cddc21bc-5508-430b-8464-ba4c836c3397`,
`0b6f0930-ffdc-4f34-869c-cca7cd8ac64b`, `4635a1f8-88f5-4ea5-b50a-69c0c6a226fb`.

CloudTrail event history expires after 90 days (around 2026-12-19 for the first
events). Save the full query output to `local-artifacts/` now.

Log excerpts: CloudTrail output above
Screenshots:
Screen recording:
Commit before (permalink): [19cc5f8](https://github.com/johnw721/Serverless-Onboarding/commit/19cc5f8) (directory still Simple AD)
Commit that fixed it (permalink): _pending_
Related docs: [Simple AD availability changes](https://docs.aws.amazon.com/directoryservice/latest/admin-guide/simple-ad-availability-change.html)

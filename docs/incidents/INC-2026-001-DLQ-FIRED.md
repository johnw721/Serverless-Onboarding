Incident ID:            INC-2026-001-adlambda-dlq-fired
Title: DLQ Fired
Status:                 Unconfirmed (see note)
Author / Date written: Jarel Wright / 09-02-2026
Environment:           prod-equivalent
Priority:

> **Note (2026-10-01):** No evidence this incident occurred. CloudWatch shows
> `NumberOfMessagesSent` = 0 for `notify-sns-dlq` and `Errors` = 0 for
> `notify_sns_function` on every day with data from May to September 2026. The DLQ
> and alarm path will be exercised deliberately in Game Day Test 1
> (`docs/GAME_DAY_TESTS.md`), and this record filled from that run.

## Impact
What broke, who or what was affected, scope, duration.
Silent or visible? Any data loss?

## Timeline
Occurred:
Detected:
Resolved:
Time to detect:
Time to resolve:

## Detection
How you found out. Alarm, log review, or noticed by chance.

## Key components involved

## Assumptions

## Symptoms

## Investigation
What you checked, in order, including the dead ends.

## Root cause

## Contributing factors
Conditions that made it possible or made it worse.

## Resolution

## Corrective actions
| Action | Owner | Status | Due |

## Evidence
Log excerpts:
Screenshots:
Screen recording:
Commit before (permalink):
Commit that fixed it (permalink):
Related KEDB article:
"""Slack dispatcher Lambda.

Sits in front of the /onboard route. Slack slash commands require an HTTP
response within 3 seconds, but the onboarding work (Bedrock parse + provision)
can take longer. This thin function:

  1. Asynchronously invokes onboarding_function (InvocationType=Event), passing
     the original API Gateway event straight through, then
  2. Immediately returns a Slack acknowledgement so the user sees instant
     feedback in the channel.

The final result is delivered back to Slack asynchronously by the existing
SNS -> slack_notifier_function path. Keeping this separate from
onboarding_function means that function's handler and its test suite are
unchanged.
"""

import json
import logging
import os

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_lambda_client = boto3.client("lambda")
_TARGET_FUNCTION = os.environ.get("ONBOARDING_FUNCTION_NAME", "onboarding_function")

_ACK_TEXT = ":hourglass_flowing_sand: Working on it — I'll post the result here shortly."


def lambda_handler(event, context):
    try:
        _lambda_client.invoke(
            FunctionName=_TARGET_FUNCTION,
            InvocationType="Event",  # async — returns immediately
            Payload=json.dumps(event).encode("utf-8"),
        )
    except ClientError as e:
        logger.error("Failed to dispatch onboarding worker: %s", e)
        return {
            "statusCode": 500,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"text": "Couldn't start onboarding — please try again."}),
        }

    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({"text": _ACK_TEXT}),
    }

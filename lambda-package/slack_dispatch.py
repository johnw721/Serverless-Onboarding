"""Slack dispatcher Lambda.

Sits in front of the /onboard and /offboard routes. Slack slash commands need
an HTTP response within 3 seconds, but the underlying work (Bedrock parse +
provision/deprovision) can take longer. This thin function:

  1. Verifies the request really came from Slack (signing-secret HMAC), then
  2. Picks the right worker from the request path (/onboard -> onboarding_function,
     /offboard -> offboarding_function) and async-invokes it (InvocationType=Event),
     passing the original API Gateway event straight through, then
  3. Immediately returns a Slack acknowledgement so the user sees instant feedback.

The final result is delivered back to Slack asynchronously by the existing
SNS -> slack_notifier_function path. Keeping this separate from the worker
functions means their handlers and test suites are unchanged.

Authentication
--------------
The /onboard and /offboard routes are public at the API Gateway level (no Lambda
authorizer). Instead this function authenticates the caller the way Slack
intends: it recomputes the HMAC-SHA256 signature over the raw request body using
the app's Slack *signing secret* and compares it to the X-Slack-Signature header.
This replaces the previous static x-api-key check, which broke silently whenever
the key was rotated and leaked the key into URL/access logs.
"""

import base64
import hashlib
import hmac
import json
import logging
import os
import time
import urllib.parse

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_lambda_client = boto3.client("lambda")
_ONBOARDING_FUNCTION = os.environ.get("ONBOARDING_FUNCTION_NAME", "onboarding_function")
_OFFBOARDING_FUNCTION = os.environ.get("OFFBOARDING_FUNCTION_NAME", "offboarding_function")

# Slack signing secret (from the app's Basic Information page). Used to verify
# every inbound request actually originated from Slack.
_SIGNING_SECRET = os.environ.get("SLACK_SIGNING_SECRET", "")

# Reject requests whose timestamp is older than this, to blunt replay attacks.
_MAX_TIMESTAMP_SKEW_SECONDS = 60 * 5

_ACK_TEXT = ":hourglass_flowing_sand: Working on it - I'll post the result here shortly."


def _raw_body_bytes(event):
    """Return the exact raw request body as bytes (the signature is computed
    over the bytes Slack sent, so we must undo any API Gateway base64 wrapping)."""
    body = event.get("body") or ""
    if event.get("isBase64Encoded"):
        return base64.b64decode(body)
    return body.encode("utf-8")


def _is_valid_slack_request(event):
    """Verify the Slack signature per
    https://api.slack.com/authentication/verifying-requests-from-slack.

    basestring = "v0:{timestamp}:{raw_body}"
    expected   = "v0=" + HMAC_SHA256(signing_secret, basestring)
    """
    if not _SIGNING_SECRET:
        logger.error("SLACK_SIGNING_SECRET is not set - rejecting all requests")
        return False

    # Header casing depends on the API Gateway payload format: 2.0 lower-cases
    # header names, 1.0 preserves the original case. Slack sends them as
    # X-Slack-Signature / X-Slack-Request-Timestamp, so normalize to lower-case
    # keys before looking them up rather than assuming a format.
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    timestamp = headers.get("x-slack-request-timestamp", "")
    slack_signature = headers.get("x-slack-signature", "")

    if not timestamp or not slack_signature:
        logger.warning("Missing Slack signature headers - rejecting request")
        return False

    try:
        if abs(time.time() - int(timestamp)) > _MAX_TIMESTAMP_SKEW_SECONDS:
            logger.warning("Slack timestamp outside allowed window - possible replay")
            return False
    except (ValueError, TypeError):
        logger.warning("Invalid Slack timestamp header - rejecting request")
        return False

    basestring = b"v0:" + timestamp.encode("utf-8") + b":" + _raw_body_bytes(event)
    expected = "v0=" + hmac.new(
        _SIGNING_SECRET.encode("utf-8"), basestring, hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(expected, slack_signature):
        logger.warning("Slack signature mismatch - rejecting request")
        return False

    return True


def _target_function(event):
    """Pick the worker to invoke based on the request path, working across HTTP
    API payload formats (2.0: rawPath / requestContext.http.path; 1.0: path /
    requestContext.path). Falls back to the Slack slash-command name in the form
    body, then defaults to onboarding for backwards compatibility.
    """
    rc = event.get("requestContext") or {}
    path = (
        event.get("rawPath")
        or event.get("path")
        or (rc.get("http") or {}).get("path")
        or rc.get("path")
        or ""
    ).lower()

    if path.endswith("/offboard"):
        return _OFFBOARDING_FUNCTION
    if path.endswith("/onboard"):
        return _ONBOARDING_FUNCTION

    # Fall back to the Slack command name in the form body (command=/offboard).
    try:
        body = _raw_body_bytes(event).decode("utf-8", "replace")
        command = (urllib.parse.parse_qs(body).get("command") or [""])[0].lower()
        if command == "/offboard":
            return _OFFBOARDING_FUNCTION
    except Exception:  # noqa: BLE001 - body parsing is best-effort here
        pass

    return _ONBOARDING_FUNCTION


def _response(status_code, text):
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({"text": text}),
    }


def lambda_handler(event, context):
    if not _is_valid_slack_request(event):
        # 401 is invisible to the channel; Slack just reports the command failed.
        return _response(401, "Unauthorized request.")

    target = _target_function(event)
    try:
        _lambda_client.invoke(
            FunctionName=target,
            InvocationType="Event",  # async - returns immediately
            Payload=json.dumps(event).encode("utf-8"),
        )
        logger.info("Dispatched request to %s", target)
    except ClientError as e:
        logger.error("Failed to dispatch worker %s: %s", target, e)
        return _response(500, "Couldn't start the request - please try again.")

    return _response(200, _ACK_TEXT)

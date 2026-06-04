"""Slack notifier Lambda.

Subscribed to the SNS notification_topic. For each SNS message it posts a
formatted notification to a Slack incoming webhook.

Design notes
------------
* This function runs OUTSIDE the VPC. The other Lambdas in this project sit in
  a private VPC with no public egress (only VPC interface endpoints), so they
  cannot reach hooks.slack.com. SNS decouples the two: the in-VPC functions
  publish to SNS, and this internet-capable function delivers to Slack.
* The webhook URL is read from an SSM SecureString parameter rather than an
  environment variable, so the secret never appears in the Lambda config or
  Terraform state in plaintext. It is cached across warm invocations.
* Only the Python standard library is used (urllib), so this handler needs no
  packaged dependencies.
"""

import json
import logging
import os
import urllib.request
import urllib.error

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_ssm_client = boto3.client("ssm")
_WEBHOOK_PARAM_NAME = os.environ.get("SLACK_WEBHOOK_SSM_PARAM", "/ad-lambda/slack-webhook-url")

# Cached across warm invocations so we don't hit SSM on every message.
_cached_webhook_url = None


def _get_webhook_url():
    global _cached_webhook_url
    if _cached_webhook_url is None:
        resp = _ssm_client.get_parameter(Name=_WEBHOOK_PARAM_NAME, WithDecryption=True)
        _cached_webhook_url = resp["Parameter"]["Value"]
    return _cached_webhook_url


def _post_to_slack(webhook_url, text):
    payload = json.dumps({"text": text}).encode("utf-8")
    req = urllib.request.Request(
        webhook_url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as response:
        body = response.read().decode("utf-8")
        if response.status != 200 or body != "ok":
            raise RuntimeError(f"Slack returned status={response.status} body={body!r}")
    return body


def lambda_handler(event, context):
    try:
        webhook_url = _get_webhook_url()
    except ClientError as e:
        logger.error("Could not read Slack webhook URL from SSM (%s): %s", _WEBHOOK_PARAM_NAME, e)
        raise

    records = event.get("Records", [])
    if not records:
        logger.warning("No SNS records in event; nothing to deliver.")
        return {"delivered": 0}

    delivered = 0
    for record in records:
        sns = record.get("Sns", {})
        subject = sns.get("Subject") or "Notification"
        message = sns.get("Message", "")
        text = f"*{subject}*\n{message}"
        try:
            _post_to_slack(webhook_url, text)
            delivered += 1
        except (urllib.error.URLError, RuntimeError) as e:
            # Raising lets Lambda's async retry + the SNS/DLQ path handle failures.
            logger.error("Failed to post to Slack: %s", e)
            raise

    logger.info("Delivered %d notification(s) to Slack.", delivered)
    return {"delivered": delivered}

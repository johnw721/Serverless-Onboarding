import boto3
import os
import logging
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

# Initialized at module level so the client is reused across warm invocations
# rather than re-created on every call.
_sns_client = boto3.client("sns")


def lambda_handler(event, context):
    topic_arn = event.get("topic_arn", os.environ.get("SNS_TOPIC_ARN"))
    message = event.get("message", "")
    subject = event.get("subject", "Notification")

    try:
        response = _sns_client.publish(
            TopicArn=topic_arn,
            Message=message,
            Subject=subject
        )
        return response
    except ClientError as e:
        logger.error(f"Failed to publish SNS notification: {e}")
        raise

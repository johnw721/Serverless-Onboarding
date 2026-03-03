import boto3
import os
from botocore.exceptions import ClientError

topic_arn = os.environ.get("SNS_TOPIC_ARN")


def notify_sns(topic_arn, message, subject):
    sns_client = boto3.client("sns")
    try:
        response = sns_client.publish(
            TopicArn=topic_arn,
            Message=message,
            Subject=subject
        )
        return response
    except ClientError as e:
        raise ClientError(
            {"Error": {"Code": "SNSPublishError", "Message": str(e)}},
            "Publish"
        )

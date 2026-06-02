import boto3
import os
import re
import time
import uuid
import json
import logging

logger = logging.getLogger(__name__)


ALL_EMPLOYEES_GROUP = "All Employees"

ROLE_TO_DEPARTMENT_MAP = {
    "Software Engineer": "Engineering",
    "Data Scientist": "Data Science",
    "Product Manager": "Product",
    "HR Specialist": "Human Resources",
    "Sales Executive": "Sales",
    "Marketing Manager": "Marketing",
    "Customer Support": "Support",
    "Finance Analyst": "Finance",
    "IT Administrator": "IT",
    "Operations Manager": "Operations",
}

ROLE_TO_GROUPS_MAP = {
    "Software Engineer": ["Engineering", ALL_EMPLOYEES_GROUP],
    "Data Scientist": ["Data Science", ALL_EMPLOYEES_GROUP],
    "Product Manager": ["Product", ALL_EMPLOYEES_GROUP],
    "HR Specialist": ["Human Resources", ALL_EMPLOYEES_GROUP],
    "Sales Executive": ["Sales", ALL_EMPLOYEES_GROUP],
    "Marketing Manager": ["Marketing", ALL_EMPLOYEES_GROUP],
    "Customer Support": ["Support", ALL_EMPLOYEES_GROUP],
    "Finance Analyst": ["Finance", ALL_EMPLOYEES_GROUP],
    "IT Administrator": ["IT", ALL_EMPLOYEES_GROUP],
    "Operations Manager": ["Operations", ALL_EMPLOYEES_GROUP],
}

# Audit log TTL: records older than this are automatically deleted by DynamoDB
_TTL_SECONDS = 365 * 24 * 60 * 60  # 1 year


def sanitize_dn_value(value: str) -> str:
    """
    Validate a value before it is interpolated into an LDAP Distinguished Name.
    Allows only alphanumeric characters, dots, hyphens, and underscores.
    Raises ValueError for anything else to prevent DN injection.
    """
    if not re.match(r'^[a-zA-Z0-9._-]+$', value):
        raise ValueError(
            f"Value '{value}' contains characters that are not permitted in an LDAP DN. "
            "Only alphanumeric characters, dots, hyphens, and underscores are allowed."
        )
    return value


def validate_employee_data(employee_data):
    """
    Validate that all required fields are present and non-null.
    Role is not checked against the hardcoded map — Claude handles novel titles.
    """
    required_fields = ["username", "name", "Role", "Department"]
    missing_fields = [
        field for field in required_fields
        if not employee_data.get(field)
    ]
    if missing_fields:
        raise ValueError(f"Missing required employee data fields: {', '.join(missing_fields)}")


def log_onboarding_request(
    employee_data: dict,
    status: str,
    groups: list | None = None,
    confidence: float | None = None,
) -> None:
    """
    Append an immutable audit record to DynamoDB.

    Each call writes a new item with a UUID partition key so that multiple
    events for the same employee (onboard, offboard, re-onboard, pending
    review) are all preserved. Using the username as the key caused silent
    overwrites — a UUID guarantees append-only behaviour.

    Optional fields:
        groups      - list of AD groups assigned/removed (omitted if None)
        confidence  - Claude's confidence score for the action (omitted if None)
    """
    now = int(time.time())
    item: dict = {
        "request_id": str(uuid.uuid4()),
        "username": employee_data["username"],
        "employee_name": employee_data.get("name", ""),
        "role": employee_data.get("Role", ""),
        "department": employee_data.get("Department", ""),
        "timestamp": now,
        "status": status,
        "ttl": now + _TTL_SECONDS,
    }
    if groups is not None:
        item["groups"] = groups
    if confidence is not None:
        item["confidence"] = str(round(confidence, 4))  # DynamoDB has no float type; store as string

    dynamodb = boto3.resource("dynamodb")
    table = dynamodb.Table(os.environ.get("DYNAMODB_TABLE_NAME"))
    try:
        table.put_item(Item=item)
    except Exception as e:
        logger.error(f"Error logging onboarding request: {str(e)}")
        raise

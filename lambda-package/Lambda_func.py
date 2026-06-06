import urllib
import boto3
import ldap3
from ldap3 import Server, Connection, ALL
from helpers import (
    validate_employee_data,
    log_onboarding_request,
    sanitize_dn_value,
    ROLE_TO_GROUPS_MAP,
)
from bedrock_agent import (
    get_confidence_threshold,
    parse_onboarding_request,
    get_group_assignments,
    generate_notification,
)
from botocore.exceptions import ClientError
from botocore.config import Config
import os
import json
import logging

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

boto3_config = Config(connect_timeout=5, read_timeout=5)

secretsmanager = boto3.client("secretsmanager", config=boto3_config)
notify_sns_lambda = boto3.client("lambda", config=boto3_config)

domain = os.getenv("DOMAIN")
base_dn = os.getenv("BASE_DN")
# Groups often live in a dedicated OU; fall back to base_dn if not set.
# Example: "OU=Groups,DC=business,DC=abc,DC=com"
group_base_dn = os.getenv("GROUP_BASE_DN", base_dn)

# Set USE_MOCK_LDAP=true to skip the real directory and log what would have
# been provisioned. Useful for demos, CI, and environments without AD.
USE_MOCK_LDAP = os.getenv("USE_MOCK_LDAP", "false").lower() == "true"

from azure_sync import sync_to_entra  # noqa: E402 — imported after env var is read

# LDAP retry settings — tune via env vars without redeploying
_LDAP_MAX_ATTEMPTS = int(os.getenv("LDAP_MAX_ATTEMPTS", "3"))
_LDAP_BACKOFF_BASE = float(os.getenv("LDAP_BACKOFF_BASE", "1.0"))


def _fetch_ldap_credentials():
    ldap_server = secretsmanager.get_secret_value(SecretId="ldap_server_address").get("SecretString")
    ldap_user = secretsmanager.get_secret_value(SecretId="ldap_username").get("SecretString")
    ldap_password = secretsmanager.get_secret_value(SecretId="ldap_password").get("SecretString")
    return ldap_server, ldap_user, ldap_password


def _extract_nl_request(event):
    raw_body = event.get("body", "")
    try:
        body_json = json.loads(raw_body)
        return body_json.get("request") or body_json.get("text") or raw_body
    except (json.JSONDecodeError, AttributeError):
        decoded = urllib.parse.unquote_plus(raw_body)
        parsed = urllib.parse.parse_qs(decoded)
        return parsed.get("text", [decoded])[0]


def _send_sns_notification(subject, message):
    try:
        notify_sns_lambda.invoke(
            FunctionName=os.getenv("NOTIFY_SNS_LAMBDA_NAME"),
            InvocationType="Event",
            Payload=json.dumps({
                "topic_arn": os.getenv("SNS_TOPIC_ARN"),
                "message": message,
                "subject": subject,
            }),
        )
    except ClientError as e:
        logger.error(f"Failed to invoke notification Lambda: {e}")


def _ldap_connect(ldap_server_addr: str, ldap_user: str, ldap_password: str):
    """
    Establish and bind an LDAP connection with exponential-backoff retry.

    Retries up to _LDAP_MAX_ATTEMPTS times on connection-level failures
    (network unreachable, timeout, etc.). Authentication errors are not
    retried — a wrong password will fail immediately on every attempt so
    we propagate the exception without sleeping.

    Returns a bound ldap3 Connection object.
    """
    import time as _time

    last_exc = None
    for attempt in range(1, _LDAP_MAX_ATTEMPTS + 1):
        try:
            server = Server(ldap_server_addr, get_info=ALL)
            conn = Connection(
                server,
                user=ldap_user,
                password=ldap_password,
                client_strategy="SYNC",
                use_ssl=True,
            )
            conn.bind()
            if attempt > 1:
                logger.info("LDAP connection succeeded on attempt %d", attempt)
            return conn
        except Exception as e:
            last_exc = e
            # Don't retry auth errors — wrong password won't fix itself
            err_lower = str(e).lower()
            if "invalid credentials" in err_lower or "unwillingtoperform" in err_lower:
                logger.error("LDAP authentication error (not retrying): %s", e)
                raise
            if attempt < _LDAP_MAX_ATTEMPTS:
                wait = _LDAP_BACKOFF_BASE * (2 ** (attempt - 1))
                logger.warning(
                    "LDAP connection attempt %d/%d failed, retrying in %.1fs: %s",
                    attempt, _LDAP_MAX_ATTEMPTS, wait, e,
                )
                _time.sleep(wait)
            else:
                logger.error(
                    "LDAP connection failed after %d attempts: %s",
                    _LDAP_MAX_ATTEMPTS, e,
                )

    raise last_exc


def _provision_real_ldap(user_info: dict, groups: list) -> dict:
    """
    Create the AD user account and add them to each group.

    User creation failure is re-raised -- it is fatal (no account = nothing to add).
    Group membership failures are caught individually: the user is still created,
    the failure is logged, and the remaining groups are attempted.

    Returns:
        {"added": list[str], "failed": list[str]}
    """
    ldap_server_addr, ldap_user, ldap_password = _fetch_ldap_credentials()
    conn = _ldap_connect(ldap_server_addr, ldap_user, ldap_password)

    # User creation is fatal -- propagate on failure
    conn.add(
        f"CN={user_info['username']},{base_dn}",
        ["top", "person", "organizationalPerson", "user"],
        {
            "sAMAccountName": user_info["username"],
            "userPrincipalName": f"{user_info['username']}@{domain}",
            "displayName": user_info["name"],
            "department": user_info.get("Department", "General"),
            "title": user_info["Role"],
        },
    )

    # Group membership -- log and continue on per-group failure
    added, failed = [], []
    for group in groups:
        try:
            conn.modify(
                f"CN={group},{group_base_dn}",
                {"member": [(ldap3.MODIFY_ADD, [f"CN={user_info['username']},{base_dn}"])]},
            )
            added.append(group)
        except Exception as e:
            logger.error(
                f"Failed to add {user_info['username']} to group '{group}': {e}"
            )
            failed.append(group)

    conn.unbind()
    return {"added": added, "failed": failed}


def _provision_mock_ldap(user_info: dict, groups: list) -> dict:
    """
    Simulate LDAP provisioning without connecting to a real directory.
    Every action that would have been taken is logged at INFO level.
    Controlled by the USE_MOCK_LDAP environment variable.
    """
    logger.info(
        "[MOCK LDAP] Would create user: CN=%s,%s | UPN=%s@%s | displayName=%s | dept=%s | title=%s",
        user_info["username"], base_dn,
        user_info["username"], domain,
        user_info.get("name"),
        user_info.get("Department", "General"),
        user_info.get("Role"),
    )
    for group in groups:
        logger.info(
            "[MOCK LDAP] Would add CN=%s,%s to group CN=%s,%s",
            user_info["username"], base_dn,
            group, group_base_dn,
        )
    return {"added": groups, "failed": []}


def lambda_handler(event, context):

    # Step 1: Extract free-form NL request from event body
    nl_request = _extract_nl_request(event)

    # Step 2: Use Claude to parse NL text into structured employee fields
    try:
        user_info = parse_onboarding_request(nl_request)
    except Exception as e:
        logger.error(f"Failed to parse onboarding request: {e}")
        return {
            "statusCode": 400,
            "body": json.dumps({"message": f"Could not parse onboarding request: {str(e)}"}),
        }

    # Step 3: Validate all required fields were extracted
    try:
        validate_employee_data(user_info)
    except ValueError as e:
        return {
            "statusCode": 400,
            "body": json.dumps({"message": str(e)}),
        }

    # Step 3b: Sanitize username before it touches any LDAP DN
    try:
        user_info["username"] = sanitize_dn_value(user_info["username"])
    except ValueError as e:
        return {
            "statusCode": 400,
            "body": json.dumps({"message": str(e)}),
        }

    # Step 4: Use Claude to determine AD group assignments with confidence score
    assignment = get_group_assignments(user_info, ROLE_TO_GROUPS_MAP)
    groups = assignment["groups"]
    confidence = assignment["confidence"]
    reasoning = assignment.get("reasoning", "")

    # Step 5a: Low confidence -- flag for manual review, return 202
    if confidence < get_confidence_threshold():
        log_onboarding_request(user_info, status="Pending Review", groups=groups, confidence=confidence)
        _send_sns_notification(
            subject="Employee Onboarding Pending Manual Review",
            message=(
                f"Onboarding request for {user_info.get('name')} "
                f"({user_info.get('Role')}) requires manual review.\n\n"
                f"Confidence: {confidence:.0%}\n"
                f"Reasoning: {reasoning}\n\n"
                f"Please review and complete the Active Directory provisioning manually."
            ),
        )
        return {
            "statusCode": 202,
            "body": json.dumps({
                "message": (
                    f"Onboarding request for {user_info.get('name')} received but requires "
                    f"manual review due to an ambiguous role description. "
                    f"The IT team has been notified."
                ),
                "username": user_info.get("username"),
                "confidence": confidence,
            }),
        }

    # Step 5b: High confidence -- provision user (real or mock)
    try:
        provision = _provision_mock_ldap if USE_MOCK_LDAP else _provision_real_ldap
        result = provision(user_info, groups)

        added = result["added"]
        failed = result["failed"]

        if failed:
            logger.warning(
                f"User {user_info['username']} created but failed to join "
                f"{len(failed)} group(s): {failed}"
            )

        # Optional: sync to Microsoft Entra ID (non-blocking)
        azure_ok, temp_password = sync_to_entra(user_info)
        if not azure_ok:
            logger.warning(
                "Entra ID sync failed for %s — AD account created but Microsoft 365 "
                "access may need manual provisioning.", user_info["username"]
            )

        status = "Partial" if failed else "Success"
        log_onboarding_request(user_info, status=status, groups=added, confidence=confidence)

        notification_message = generate_notification(user_info, added, status)
        if temp_password:
            notification_message += (
                f"\n\nEntra ID account created. Temporary password: {temp_password}"
                "\n(Employee must change on first sign-in. Communicate via secure channel.)"
            )
        _send_sns_notification(
            subject=f"New Employee Onboarding {status}",
            message=notification_message,
        )

        body = {
            "message": f"User {user_info['username']} onboarded successfully.",
            "groups_added": added,
        }
        if failed:
            body["groups_failed"] = failed
            body["message"] += (
                f" Warning: failed to join {len(failed)} group(s). "
                "Manual assignment required."
            )

        return {"statusCode": 200, "body": json.dumps(body)}

    except Exception as e:
        logger.error(f"Onboarding failed for {user_info.get('username', 'unknown')}: {e}")
        log_onboarding_request(user_info, status="Failed", groups=groups, confidence=confidence)

        notification_message = generate_notification(user_info, groups, "Failed")
        _send_sns_notification(
            subject="New Employee Onboarding Failure",
            message=notification_message,
        )

        return {
            "statusCode": 500,
            "body": json.dumps({"message": f"Failed to onboard user. Error: {str(e)}"}),
        }

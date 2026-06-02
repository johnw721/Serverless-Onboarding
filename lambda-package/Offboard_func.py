"""
Offboarding Lambda handler.

Accepts a free-form natural language request identifying the employee to
offboard (e.g. "Please offboard John Doe" or just "jdoe").

Steps:
  1. Claude extracts the username/name from the NL request
  2. LDAP: disable account (userAccountControl = 514) + remove from all groups
  3. Entra ID: disable account via Graph API (if AZURE_SYNC_ENABLED)
  4. DynamoDB: log with status "Offboarded"
  5. SNS: send notification via notify_sns_function (async)

Supports USE_MOCK_LDAP=true for demo/CI use without a real directory.
"""
import urllib
import boto3
import ldap3
from ldap3 import Server, Connection, ALL, MODIFY_REPLACE
from helpers import log_onboarding_request, sanitize_dn_value
from bedrock_agent import (
    _invoke_claude,
    _strip_fences,
    generate_notification,
    get_offboard_confidence_threshold,
)
from azure_sync import deprovision_from_entra
from botocore.exceptions import ClientError
from botocore.config import Config
import json
import logging
import os

logger = logging.getLogger(__name__)

boto3_config = Config(connect_timeout=5, read_timeout=5)

secretsmanager = boto3.client("secretsmanager", config=boto3_config)
notify_sns_lambda = boto3.client("lambda", config=boto3_config)

domain = os.getenv("DOMAIN")
base_dn = os.getenv("BASE_DN")
group_base_dn = os.getenv("GROUP_BASE_DN", base_dn)

USE_MOCK_LDAP = os.getenv("USE_MOCK_LDAP", "false").lower() == "true"

# LDAP retry settings — tune via env vars without redeploying
_LDAP_MAX_ATTEMPTS = int(os.getenv("LDAP_MAX_ATTEMPTS", "3"))
_LDAP_BACKOFF_BASE = float(os.getenv("LDAP_BACKOFF_BASE", "1.0"))

# userAccountControl value for a disabled AD account
_AD_ACCOUNT_DISABLED = 514


def _ldap_connect(ldap_server_addr: str, ldap_user: str, ldap_password: str):
    """
    Establish and bind an LDAP connection with exponential-backoff retry.

    Retries up to _LDAP_MAX_ATTEMPTS times on connection-level failures.
    Authentication errors are not retried — propagated immediately.
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


def _extract_request(event) -> str:
    raw_body = event.get("body", "")
    try:
        body_json = json.loads(raw_body)
        return body_json.get("request") or body_json.get("text") or raw_body
    except (json.JSONDecodeError, AttributeError):
        decoded = urllib.parse.unquote_plus(raw_body)
        parsed = urllib.parse.parse_qs(decoded)
        return parsed.get("text", [decoded])[0]


def _parse_offboard_request(free_form_text: str) -> dict:
    """
    Use Claude to extract the username, full name, and identity confidence
    from a free-form offboarding request.

    Returns dict with keys: username, name, confidence.
    confidence is 0.0-1.0: how certain Claude is it has identified the right
    individual. Used to gate auto-offboarding vs. routing to manual review.
    """
    prompt = f"""Extract the employee to be offboarded from the request below.
Return a JSON object with exactly these keys:
  "username"   - lowercase first initial + last name (e.g. "jdoe"), or null if not determinable
  "name"       - full name, or null if not stated
  "confidence" - float 0.0-1.0: how certain you are this identifies a unique, specific individual
                 1.0  = exact username or unambiguous full name provided
                 0.95 = full name provided, no ambiguity
                 0.7-0.94 = partial name or common name that could match multiple people
                 < 0.7 = nickname, first name only, or genuinely ambiguous description

Request:
{free_form_text}

Return only valid JSON. No explanation, no markdown."""

    text = _invoke_claude(prompt)
    parsed = json.loads(_strip_fences(text))
    parsed.setdefault("username", None)
    parsed.setdefault("name", None)
    parsed.setdefault("confidence", 0.0)
    return parsed


def _fetch_ldap_credentials():
    ldap_server = secretsmanager.get_secret_value(SecretId="ldap_server_address")["SecretString"]
    ldap_user = secretsmanager.get_secret_value(SecretId="ldap_username")["SecretString"]
    ldap_password = secretsmanager.get_secret_value(SecretId="ldap_password")["SecretString"]
    return ldap_server, ldap_user, ldap_password


def _send_sns_notification(subject: str, message: str):
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
        logger.error("Failed to invoke notification Lambda: %s", e)


def _offboard_real_ldap(username: str) -> dict:
    """
    Disable the AD account and remove it from all groups.

    Returns {"disabled": bool, "groups_removed": list[str], "groups_failed": list[str]}
    """
    ldap_server_addr, ldap_user, ldap_password = _fetch_ldap_credentials()
    conn = _ldap_connect(ldap_server_addr, ldap_user, ldap_password)

    user_dn = f"CN={username},{base_dn}"

    # Disable account — fatal if it fails (account doesn't exist or wrong DN)
    conn.modify(user_dn, {"userAccountControl": [(MODIFY_REPLACE, [_AD_ACCOUNT_DISABLED])]})

    # Find all groups the user is a member of.
    # conn.response is a list of raw dicts; filter to searchResEntry to skip
    # referrals and the searchResDone control entry.
    conn.search(
        search_base=group_base_dn,
        search_filter=f"(&(objectClass=group)(member={user_dn}))",
        attributes=["cn"],
    )
    groups = [
        e["attributes"]["cn"]
        for e in conn.response
        if e.get("type") == "searchResEntry"
    ]

    removed, failed = [], []
    for group in groups:
        try:
            conn.modify(
                f"CN={group},{group_base_dn}",
                {"member": [(ldap3.MODIFY_DELETE, [user_dn])]},
            )
            removed.append(group)
        except Exception as e:
            logger.error("Failed to remove %s from group '%s': %s", username, group, e)
            failed.append(group)

    conn.unbind()
    return {"disabled": True, "groups_removed": removed, "groups_failed": failed}


def _offboard_mock_ldap(username: str) -> dict:
    """Simulate offboarding without connecting to a real directory."""
    logger.info("[MOCK LDAP] Would disable account: CN=%s,%s (userAccountControl=514)", username, base_dn)
    logger.info("[MOCK LDAP] Would search for group memberships of CN=%s,%s", username, base_dn)
    logger.info("[MOCK LDAP] Would remove CN=%s,%s from all found groups", username, base_dn)
    return {"disabled": True, "groups_removed": ["All Employees"], "groups_failed": []}


def lambda_handler(event, context):

    # Step 1: Extract NL request
    nl_request = _extract_request(event)

    # Step 2: Claude parses who to offboard
    try:
        target = _parse_offboard_request(nl_request)
    except Exception as e:
        logger.error("Failed to parse offboard request: %s", e)
        return {
            "statusCode": 400,
            "body": json.dumps({"message": f"Could not parse offboarding request: {e}"}),
        }

    username = target.get("username")
    name = target.get("name") or username

    if not username:
        return {
            "statusCode": 400,
            "body": json.dumps({"message": "Could not determine username from request."}),
        }

    # Step 3: Sanitize username
    try:
        username = sanitize_dn_value(username)
    except ValueError as e:
        return {
            "statusCode": 400,
            "body": json.dumps({"message": str(e)}),
        }

    # Build a minimal user_info dict for logging and notification
    user_info = {"username": username, "name": name, "Role": "N/A", "Department": "N/A"}

    # Step 4: Confidence gate — offboarding is destructive, so the bar is higher
    # than onboarding. Ambiguous requests are held for manual review.
    confidence = target.get("confidence", 0.0)
    if confidence < get_offboard_confidence_threshold():
        log_onboarding_request(user_info, status="Offboard Pending Review", confidence=confidence)
        _send_sns_notification(
            subject="Employee Offboarding Pending Manual Review",
            message=(
                f"Offboarding request could not be auto-processed — identity is ambiguous.\n\n"
                f"Original request: {nl_request!r}\n"
                f"Claude extracted: username={username!r}, name={name!r}\n"
                f"Confidence: {confidence:.0%}\n\n"
                f"Please verify the identity and offboard manually if correct."
            ),
        )
        return {
            "statusCode": 202,
            "body": json.dumps({
                "message": (
                    f"Offboarding request for '{username}' requires manual review — "
                    f"the identity could not be confirmed with sufficient confidence. "
                    f"The IT team has been notified."
                ),
                "username": username,
                "confidence": confidence,
            }),
        }

    # Step 5: LDAP offboarding
    try:
        offboard = _offboard_mock_ldap if USE_MOCK_LDAP else _offboard_real_ldap
        result = offboard(username)
    except Exception as e:
        logger.error("LDAP offboarding failed for %s: %s", username, e)
        log_onboarding_request(user_info, status="Offboard Failed", confidence=confidence)
        _send_sns_notification(
            subject="Employee Offboarding Failure",
            message=(
                f"Failed to offboard {name} ({username}). "
                f"Manual intervention required. Check CloudWatch logs for details."
            ),
        )
        return {
            "statusCode": 500,
            "body": json.dumps({"message": f"Offboarding failed for {username}: {e}"}),
        }

    # Step 6: Entra ID deprovisioning (non-blocking)
    azure_ok = deprovision_from_entra(username)
    if not azure_ok:
        logger.warning("Entra ID deprovisioning failed for %s — manual action may be needed.", username)

    # Step 7: Log and notify
    status = "Offboarded" if not result["groups_failed"] else "Offboarded (Partial)"
    log_onboarding_request(user_info, status=status, groups=result["groups_removed"], confidence=confidence)

    notification = generate_notification(user_info, result["groups_removed"], status)
    _send_sns_notification(subject=f"Employee Offboarding {status}", message=notification)

    body: dict = {
        "message": f"User {username} offboarded successfully.",
        "account_disabled": result["disabled"],
        "groups_removed": result["groups_removed"],
    }
    if result["groups_failed"]:
        body["groups_failed"] = result["groups_failed"]
        body["message"] += f" Warning: failed to remove from {len(result['groups_failed'])} group(s)."
    if not azure_ok:
        body["azure_sync_warning"] = "Entra ID deprovisioning failed — manual action required."

    return {"statusCode": 200, "body": json.dumps(body)}

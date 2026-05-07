"""
Azure AD / Entra ID synchronization via Microsoft Graph API.

Reads credentials from Secrets Manager (lazily, cached for the lifetime of
the execution environment). Tokens are cached with a TTL so each Lambda
invocation makes at most one token request per expiry window.

All public functions return a bool and never raise — failures are logged
and treated as non-fatal so a Graph API outage cannot block onboarding.

Enable by setting AZURE_SYNC_ENABLED=true in the Lambda environment.
"""
import json
import logging
import os
import time
import urllib.request
import urllib.parse
import urllib.error

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

AZURE_SYNC_ENABLED = os.environ.get("AZURE_SYNC_ENABLED", "false").lower() == "true"

_GRAPH_BASE = "https://graph.microsoft.com/v1.0"
_TOKEN_URL_TEMPLATE = "https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
_SCOPE = "https://graph.microsoft.com/.default"

# Secret names in AWS Secrets Manager
_SECRET_TENANT_ID = "azure_tenant_id"
_SECRET_CLIENT_ID = "azure_client_id"
_SECRET_CLIENT_SECRET = "azure_client_secret"

# ── Lazy-initialized clients and caches ──────────────────────────────────────

_sm_client = None
_creds: dict = {"tenant_id": None, "client_id": None, "client_secret": None}
_token_cache: dict = {"token": None, "expires_at": 0.0}


def _get_sm_client():
    global _sm_client
    if _sm_client is None:
        _sm_client = boto3.client(
            "secretsmanager",
            config=Config(connect_timeout=5, read_timeout=5),
        )
    return _sm_client


def _load_creds() -> bool:
    """Fetch Azure credentials from Secrets Manager once per cold start."""
    if _creds["tenant_id"]:
        return True
    try:
        sm = _get_sm_client()
        _creds["tenant_id"] = sm.get_secret_value(SecretId=_SECRET_TENANT_ID)["SecretString"]
        _creds["client_id"] = sm.get_secret_value(SecretId=_SECRET_CLIENT_ID)["SecretString"]
        _creds["client_secret"] = sm.get_secret_value(SecretId=_SECRET_CLIENT_SECRET)["SecretString"]
        return True
    except ClientError as e:
        logger.error("Failed to load Azure credentials from Secrets Manager: %s", e)
        return False


def _get_access_token() -> str | None:
    """Return a valid Graph API access token, refreshing if within 60s of expiry."""
    now = time.monotonic()
    if _token_cache["token"] and now < _token_cache["expires_at"] - 60:
        return _token_cache["token"]

    if not _load_creds():
        return None

    url = _TOKEN_URL_TEMPLATE.format(tenant_id=_creds["tenant_id"])
    data = urllib.parse.urlencode({
        "grant_type": "client_credentials",
        "client_id": _creds["client_id"],
        "client_secret": _creds["client_secret"],
        "scope": _SCOPE,
    }).encode()

    try:
        req = urllib.request.Request(url, data=data, method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = json.loads(resp.read())
        _token_cache["token"] = body["access_token"]
        _token_cache["expires_at"] = now + body.get("expires_in", 3600)
        return _token_cache["token"]
    except Exception as e:
        logger.error("Failed to obtain Graph API access token: %s", e)
        return None


def _graph_request(method: str, path: str, payload: dict | None = None) -> dict | None:
    """Make a single Graph API request. Returns parsed JSON or None on failure."""
    token = _get_access_token()
    if not token:
        return None

    url = f"{_GRAPH_BASE}{path}"
    body = json.dumps(payload).encode() if payload else None
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        logger.error("Graph API %s %s returned %s: %s", method, path, e.code, e.read())
        return None
    except Exception as e:
        logger.error("Graph API %s %s failed: %s", method, path, e)
        return None


# ── Public API ────────────────────────────────────────────────────────────────

def sync_to_entra(user_info: dict) -> tuple:
    """
    Create the user in Microsoft Entra ID (Azure AD) via Graph API.

    Maps AD fields to Graph user properties. The account is created in
    enabled state with a one-time random password that must be changed on
    first sign-in. The caller should surface the password in the IT
    notification so admins can distribute it through a secure channel.

    Returns:
        (True, temp_password)  on success
        (False, None)          on any failure
    Never raises.
    """
    if not AZURE_SYNC_ENABLED:
        logger.debug("Azure sync disabled — skipping Entra ID provisioning.")
        return True, None

    username = user_info.get("username", "")
    name = user_info.get("name", "")
    domain = os.environ.get("DOMAIN", "")
    upn = f"{username}@{domain}"
    temp_password = _generate_temp_password()

    payload = {
        "accountEnabled": True,
        "displayName": name,
        "mailNickname": username,
        "userPrincipalName": upn,
        "jobTitle": user_info.get("Role"),
        "department": user_info.get("Department"),
        "passwordProfile": {
            "forceChangePasswordNextSignIn": True,
            "password": temp_password,
        },
    }

    result = _graph_request("POST", "/users", payload)
    if result is None:
        logger.error("Entra ID provisioning failed for %s", username)
        return False, None

    logger.info("Entra ID user created: %s (UPN: %s)", name, upn)
    return True, temp_password


def deprovision_from_entra(username: str) -> bool:
    """
    Disable the user in Entra ID without deleting the account (preserves audit trail).

    Returns True on success, False on any failure. Never raises.
    """
    if not AZURE_SYNC_ENABLED:
        logger.debug("Azure sync disabled — skipping Entra ID deprovisioning.")
        return True

    # Look up the user by UPN to get their object ID
    domain = os.environ.get("DOMAIN", "")
    upn = f"{username}@{domain}"
    user_obj = _graph_request("GET", f"/users/{upn}")
    if user_obj is None:
        logger.error("Could not find Entra ID user %s for deprovisioning", upn)
        return False

    object_id = user_obj.get("id")
    result = _graph_request("PATCH", f"/users/{object_id}", {"accountEnabled": False})
    if result is None:
        logger.error("Failed to disable Entra ID account for %s", username)
        return False

    logger.info("Entra ID account disabled for %s (%s)", username, upn)
    return True


def _generate_temp_password() -> str:
    """
    Generate a cryptographically random temporary password that meets
    common Active Directory complexity requirements:
    at least one uppercase, lowercase, digit, and special character.

    Uses secrets.token_urlsafe for the random body — not deterministic,
    not derivable. The caller includes it in the IT notification so admins
    can distribute it to the new hire through a secure out-of-band channel.
    """
    import secrets
    import string
    # 16-char random alphanumeric base
    alphabet = string.ascii_letters + string.digits
    body = "".join(secrets.choice(alphabet) for _ in range(16))
    # Prepend fixed complexity chars to guarantee policy compliance
    # (uppercase + special + digit are all covered regardless of body)
    return f"T!{secrets.choice(string.digits)}{body}"

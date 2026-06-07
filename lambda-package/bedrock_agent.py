import boto3
import json
import logging
import os
import time
from botocore.config import Config
from botocore.exceptions import ClientError
from helpers import resolve_department_group, ALL_EMPLOYEES_GROUP

logger = logging.getLogger(__name__)

# ---- Bedrock ----

MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "us.anthropic.claude-haiku-4-5-20251001-v1:0")

# Lazy-initialized -- not created at module load time so a cold start never
# fails due to missing credentials or an unreachable endpoint before the
# handler is even called.
_bedrock_runtime = None


def _get_bedrock_client():
    global _bedrock_runtime
    if _bedrock_runtime is None:
        # Bound the call so a hung/throttled model can't run the whole Lambda to
        # its 60s timeout before the callers' fallbacks engage. Worst case per
        # call is connect_timeout + read_timeout * max_attempts ~ 5 + 10*2 = 25s,
        # leaving budget for the template fallback and the SNS notification.
        _bedrock_runtime = boto3.client(
            "bedrock-runtime",
            config=Config(
                connect_timeout=5,
                read_timeout=10,
                retries={"max_attempts": 2, "mode": "standard"},
            ),
        )
    return _bedrock_runtime


def _invoke_claude(prompt: str, max_tokens: int = 1024) -> str:
    """Invoke Claude via Bedrock Runtime and return the raw text response."""
    body = json.dumps({
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    })
    response = _get_bedrock_client().invoke_model(modelId=MODEL_ID, body=body)
    result = json.loads(response["body"].read())
    return result["content"][0]["text"]


def _strip_fences(text: str) -> str:
    """Strip markdown code fences that Claude sometimes wraps JSON in."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
    if text.endswith("```"):
        text = text.rsplit("```", 1)[0]
    return text.strip()


# ---- Confidence threshold (SSM-backed, TTL-cached) ----

_CONFIDENCE_THRESHOLD_DEFAULT = 0.8
_CONFIDENCE_THRESHOLD_SSM_PARAM = os.environ.get(
    "CONFIDENCE_THRESHOLD_SSM_PARAM", "/ad-lambda/confidence-threshold"
)
_CACHE_TTL_SECONDS = 300  # 5 minutes

_ssm_client = None
_threshold_cache: dict = {"value": None, "loaded_at": 0.0}


def _get_ssm_client():
    global _ssm_client
    if _ssm_client is None:
        # Bounded so an unreachable SSM endpoint fails fast and get_confidence_threshold
        # falls back to the default/cached value instead of hanging the whole Lambda.
        _ssm_client = boto3.client(
            "ssm",
            config=Config(connect_timeout=2, read_timeout=2, retries={"max_attempts": 2}),
        )
    return _ssm_client


def get_confidence_threshold() -> float:
    """
    Return the current confidence threshold.

    On first call (or after the 5-minute TTL expires) the value is fetched
    from SSM Parameter Store so ops can adjust it without a Lambda redeployment.
    The cached value is served for all subsequent calls within the TTL window.

    Falls back to 0.8 if SSM is unreachable or the parameter is missing,
    using the last successfully cached value if one exists.
    """
    now = time.monotonic()
    cache = _threshold_cache

    # Serve from cache if still fresh
    if cache["value"] is not None and (now - cache["loaded_at"]) < _CACHE_TTL_SECONDS:
        return cache["value"]

    try:
        response = _get_ssm_client().get_parameter(Name=_CONFIDENCE_THRESHOLD_SSM_PARAM)
        value = float(response["Parameter"]["Value"])
        if not 0.0 <= value <= 1.0:
            raise ValueError(f"Threshold {value} is outside the valid range [0.0, 1.0]")
        logger.info(
            "Refreshed confidence threshold: %.2f (SSM param: %s)",
            value, _CONFIDENCE_THRESHOLD_SSM_PARAM,
        )
        cache["value"] = value
        cache["loaded_at"] = now
        return value

    except Exception as e:
        if cache["value"] is not None:
            logger.warning(
                "SSM read failed -- serving cached threshold %.2f: %s",
                cache["value"], e,
            )
            return cache["value"]
        logger.warning(
            "SSM read failed and no cached value -- using default %.2f: %s",
            _CONFIDENCE_THRESHOLD_DEFAULT, e,
        )
        return _CONFIDENCE_THRESHOLD_DEFAULT


# ---- Offboard confidence threshold (separate SSM param, higher default) ----

_OFFBOARD_CONFIDENCE_THRESHOLD_DEFAULT = 0.95
_OFFBOARD_CONFIDENCE_THRESHOLD_SSM_PARAM = os.environ.get(
    "OFFBOARD_CONFIDENCE_THRESHOLD_SSM_PARAM", "/ad-lambda/offboard-confidence-threshold"
)
_offboard_threshold_cache: dict = {"value": None, "loaded_at": 0.0}


def get_offboard_confidence_threshold() -> float:
    """
    Return the current offboard confidence threshold (default 0.95).

    Stored in a separate SSM parameter from the onboarding threshold so ops
    can tune each independently. The higher default (0.95 vs 0.8) reflects
    that offboarding is destructive and harder to undo than onboarding.

    Falls back to 0.95 on SSM failure, using the last cached value if available.
    """
    now = time.monotonic()
    cache = _offboard_threshold_cache

    if cache["value"] is not None and (now - cache["loaded_at"]) < _CACHE_TTL_SECONDS:
        return cache["value"]

    try:
        response = _get_ssm_client().get_parameter(Name=_OFFBOARD_CONFIDENCE_THRESHOLD_SSM_PARAM)
        value = float(response["Parameter"]["Value"])
        if not 0.0 <= value <= 1.0:
            raise ValueError(f"Threshold {value} is outside the valid range [0.0, 1.0]")
        logger.info(
            "Refreshed offboard confidence threshold: %.2f (SSM param: %s)",
            value, _OFFBOARD_CONFIDENCE_THRESHOLD_SSM_PARAM,
        )
        cache["value"] = value
        cache["loaded_at"] = now
        return value

    except Exception as e:
        if cache["value"] is not None:
            logger.warning(
                "SSM read failed -- serving cached offboard threshold %.2f: %s",
                cache["value"], e,
            )
            return cache["value"]
        logger.warning(
            "SSM read failed and no cached value -- using default %.2f: %s",
            _OFFBOARD_CONFIDENCE_THRESHOLD_DEFAULT, e,
        )
        return _OFFBOARD_CONFIDENCE_THRESHOLD_DEFAULT


# ---- Claude functions ----

def parse_onboarding_request(free_form_text: str) -> dict:
    """
    Use Claude to extract structured employee data from a free-form onboarding request.
    Returns a dict with keys: username, name, Role, Department.
    Raises ValueError if Claude cannot produce a usable result.
    """
    prompt = f"""Extract employee onboarding details from the request below.
Return a JSON object with exactly these keys:
  "username"   - lowercase first initial + last name (e.g. "jdoe" for John Doe)
  "name"       - full name
  "Role"       - job title exactly as stated, or closest reasonable interpretation
  "Department" - the department name ONLY if it is explicitly stated in the request.
                 If the request does not explicitly name a department, use null.
                 Do NOT infer, guess, or derive the department from the job title.

If a field truly cannot be determined, use null.

Request:
{free_form_text}

Return only valid JSON. No explanation, no markdown."""

    text = _invoke_claude(prompt)
    parsed = json.loads(_strip_fences(text))

    for key in ("username", "name", "Role", "Department"):
        parsed.setdefault(key, None)

    return parsed


def get_group_assignments(user_info: dict, fallback_map: dict) -> dict:
    """
    Decide which AD security groups an employee should be added to.

    Department-driven: the employee's DEPARTMENT determines their primary group
    (plus "All Employees"). Job-title keywords are deliberately NOT used to infer
    a department group -- a "QA Engineer" in Finance must land in Finance, never
    Engineering. When the department resolves to a known group this is fully
    deterministic (confidence 1.0) and Claude is not called.

    If no department was specified, the request is routed to manual review
    (confidence 0.0) rather than guessing. Claude is consulted only when a
    department was given but does not resolve to a known group.

    Returns: {"groups": list[str], "confidence": float, "reasoning": str}
    """
    department = (user_info.get("Department") or "").strip()

    # No department stated -> do not guess; route to manual review.
    if not department:
        logger.info("No department specified; routing onboarding to manual review.")
        return {
            "groups": [ALL_EMPLOYEES_GROUP],
            "confidence": 0.0,
            "reasoning": (
                "No department was specified in the request, so a department group "
                "cannot be assigned automatically. Routing to manual review."
            ),
        }

    group = resolve_department_group(department)
    if group:
        groups = [group] if group == ALL_EMPLOYEES_GROUP else [group, ALL_EMPLOYEES_GROUP]
        return {
            "groups": groups,
            "confidence": 1.0,
            "reasoning": (
                f"Department '{department}' maps to the '{group}' group "
                f"(department-driven; job title is not used to infer groups)."
            ),
        }

    logger.info("Department '%s' did not resolve to a known group; asking Claude.", department)
    return _claude_group_assignments(user_info, fallback_map)


def _claude_group_assignments(user_info: dict, fallback_map: dict) -> dict:
    """
    LLM fallback for employees whose department does not resolve to a known group.

    Still department-first: the prompt forbids inferring a group from a keyword in
    the job title. Falls back to the hardcoded role map on a Bedrock error, or to
    ["All Employees"] with confidence 0.0 if the role is unknown too.
    """
    role = user_info.get("Role", "")
    department = user_info.get("Department", "")
    all_groups = sorted(set(g for groups in fallback_map.values() for g in groups))

    prompt = f"""Determine which Active Directory security groups an employee should be added to.

Employee role:       "{role}"
Employee department: "{department}"

Available groups: {json.dumps(all_groups)}

Rules:
- Base the decision on the employee's DEPARTMENT, not on keywords in their job title.
- Never add a department group just because the job title contains that word. For
  example, a "QA Engineer" in Finance belongs to Finance, NOT Engineering.
- Always include "All Employees".
- Pick at most one department group: the one matching the employee's department.
- If the department clearly matches an available group, confidence 0.9-1.0.
- If it is ambiguous or novel but inferable, use 0.5-0.89.
- If you genuinely cannot map the department, use confidence 0.0 and groups ["All Employees"].

Return only valid JSON:
{{
  "groups": ["Group A"],
  "confidence": 0.95,
  "reasoning": "One sentence explaining the decision."
}}"""

    try:
        text = _invoke_claude(prompt)
        result = json.loads(_strip_fences(text))

        if ALL_EMPLOYEES_GROUP not in result.get("groups", []):
            result.setdefault("groups", []).append(ALL_EMPLOYEES_GROUP)

        return result

    except Exception as e:
        logger.warning(f"Claude group assignment failed, falling back to hardcoded map: {e}")
        fallback_groups = fallback_map.get(role, [ALL_EMPLOYEES_GROUP])
        return {
            "groups": fallback_groups,
            "confidence": 1.0 if role in fallback_map else 0.0,
            "reasoning": "Used hardcoded fallback map due to Bedrock error.",
        }


def generate_notification(user_info: dict, groups: list, status: str) -> str:
    """
    Use Claude to generate a rich, context-aware onboarding notification.
    Falls back to a plain template if Bedrock is unavailable.
    """
    name = user_info.get("name", "Unknown")
    username = user_info.get("username", "unknown")
    role = user_info.get("Role", "Unknown")
    department = user_info.get("Department", "Unknown")
    groups_str = ", ".join(groups) if groups else "none"

    prompt = f"""Write a brief, professional IT notification about an employee onboarding event.

Details:
  Name:       {name}
  Username:   {username}
  Role:       {role}
  Department: {department}
  AD groups:  {groups_str}
  Status:     {status}

Guidelines:
- 2-3 sentences maximum.
- Be specific: name the employee, their role, and what was done.
- If status is "Failed", note that manual intervention is required.
- If status is "Partial", note that some groups require manual assignment.
- If status is "Success", optionally mention relevant systems for the role.
- Do not include a subject line. Return only the message body."""

    try:
        return _invoke_claude(prompt, max_tokens=256).strip()
    except Exception as e:
        logger.warning(f"Claude notification generation failed, using template: {e}")
        if status == "Success":
            return (
                f"Successfully onboarded {name} ({username}) as {role} in {department}. "
                f"Active Directory account created and added to: {groups_str}."
            )
        if status == "Partial":
            return (
                f"Partially onboarded {name} ({username}) as {role} in {department}. "
                f"Account created and added to: {groups_str}. "
                f"Some group assignments failed and require manual intervention."
            )
        return (
            f"Failed to onboard {name} ({username}) as {role} in {department}. "
            f"Manual intervention is required. Please review the CloudWatch logs for details."
        )

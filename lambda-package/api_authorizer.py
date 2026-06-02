import os
import logging

logger = logging.getLogger(__name__)

# Read once at cold start — the key is static and only changes with a redeploy
_API_KEY = os.environ.get("ONBOARDING_API_KEY", "")


def lambda_handler(event, context):
    """
    Lambda REQUEST authorizer for the onboarding HTTP API.
    Checks the x-api-key header against the ONBOARDING_API_KEY env var.
    Returns the simple response format required by API Gateway HTTP API authorizers.
    """
    if not _API_KEY:
        logger.error("ONBOARDING_API_KEY is not set — denying all requests")
        return {"isAuthorized": False}

    headers = event.get("headers", {})
    query_params = event.get("queryStringParameters") or {}
    provided_key = headers.get("x-api-key") or query_params.get("x-api-key", "")

    authorized = provided_key == _API_KEY
    if not authorized:
        logger.warning("Unauthorized request — invalid or missing x-api-key header")

    return {"isAuthorized": authorized}

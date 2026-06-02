"""
End-to-end handler tests for Lambda_func.lambda_handler.

LDAP is bypassed via USE_MOCK_LDAP=true.
All AWS calls are mocked — no live infrastructure required.

Patch targets use Lambda_func.* because the handler imports symbols
directly (from helpers import log_onboarding_request, etc.), so
the copies in Lambda_func's namespace must be patched, not the originals.
"""
import json
import os
import sys
import unittest
from unittest.mock import patch, MagicMock

# ── All env vars must be set before Lambda_func is imported ──────────────────
os.environ.setdefault("USE_MOCK_LDAP", "true")
os.environ.setdefault("AZURE_SYNC_ENABLED", "false")
os.environ.setdefault("DOMAIN", "business.abc.com")
os.environ.setdefault("BASE_DN", "DC=business,DC=abc,DC=com")
os.environ.setdefault("GROUP_BASE_DN", "OU=Groups,DC=business,DC=abc,DC=com")
os.environ.setdefault("SNS_TOPIC_ARN", "arn:aws:sns:us-west-2:123456789012:test-topic")
os.environ.setdefault("NOTIFY_SNS_LAMBDA_NAME", "notify_sns_function")
os.environ.setdefault("DYNAMODB_TABLE_NAME", "onboarding_request_table")
os.environ.setdefault("CONFIDENCE_THRESHOLD_SSM_PARAM", "/ad-lambda/confidence-threshold")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-west-2")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "test-key-id")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "test-secret-key")
os.environ.setdefault("AWS_EC2_METADATA_DISABLED", "true")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lambda-package"))

import Lambda_func  # noqa: E402


def _event(body: dict) -> dict:
    return {"body": json.dumps(body)}


class TestLambdaHandlerE2E(unittest.TestCase):
    """
    Full handler orchestration tests. Each test patches only the symbols
    needed to isolate that code path.
    """

    def setUp(self):
        # Replace module-level boto3 clients — created at import time before
        # we can patch them, so swap them out directly on the module.
        self.mock_secretsmanager = MagicMock()
        self.mock_notify_lambda = MagicMock()
        Lambda_func.secretsmanager = self.mock_secretsmanager
        Lambda_func.notify_sns_lambda = self.mock_notify_lambda

    # ── 200 Happy Path ────────────────────────────────────────────────────────

    @patch("Lambda_func.log_onboarding_request")
    @patch("Lambda_func.get_confidence_threshold", return_value=0.8)
    @patch("Lambda_func.generate_notification", return_value="Alice onboarded.")
    @patch("Lambda_func.get_group_assignments")
    @patch("Lambda_func.parse_onboarding_request")
    def test_200_happy_path(
        self, mock_parse, mock_groups, mock_notify, mock_threshold, mock_log
    ):
        mock_parse.return_value = {
            "username": "asmith",
            "name": "Alice Smith",
            "Role": "Software Engineer",
            "Department": "Engineering",
        }
        mock_groups.return_value = {
            "groups": ["Engineering", "All Employees"],
            "confidence": 0.95,
            "reasoning": "Clear role match.",
        }

        resp = Lambda_func.lambda_handler(
            _event({"request": "Please onboard Alice Smith as a Software Engineer"}), {}
        )

        self.assertEqual(resp["statusCode"], 200)
        body = json.loads(resp["body"])
        self.assertIn("asmith", body["message"])
        self.assertIn("Engineering", body["groups_added"])
        self.assertNotIn("groups_failed", body)
        mock_log.assert_called_once_with(
            {"username": "asmith", "name": "Alice Smith",
             "Role": "Software Engineer", "Department": "Engineering"},
            status="Success",
            groups=["Engineering", "All Employees"],
            confidence=0.95,
        )
        self.mock_notify_lambda.invoke.assert_called_once()

    # ── 202 Low Confidence ────────────────────────────────────────────────────

    @patch("Lambda_func.log_onboarding_request")
    @patch("Lambda_func.get_confidence_threshold", return_value=0.8)
    @patch("Lambda_func.get_group_assignments")
    @patch("Lambda_func.parse_onboarding_request")
    def test_202_low_confidence_routes_to_manual_review(
        self, mock_parse, mock_groups, mock_threshold, mock_log
    ):
        mock_parse.return_value = {
            "username": "bwilson",
            "name": "Bob Wilson",
            "Role": "Innovation Catalyst",
            "Department": "Strategy",
        }
        mock_groups.return_value = {
            "groups": ["All Employees"],
            "confidence": 0.45,
            "reasoning": "Role too ambiguous.",
        }

        resp = Lambda_func.lambda_handler(
            _event({"request": "Onboard Bob Wilson as Innovation Catalyst"}), {}
        )

        self.assertEqual(resp["statusCode"], 202)
        body = json.loads(resp["body"])
        self.assertIn("manual review", body["message"])
        self.assertAlmostEqual(body["confidence"], 0.45)
        mock_log.assert_called_once_with(
            {"username": "bwilson", "name": "Bob Wilson",
             "Role": "Innovation Catalyst", "Department": "Strategy"},
            status="Pending Review",
            groups=["All Employees"],
            confidence=0.45,
        )
        # SNS alert must be fired; LDAP must NOT be touched
        self.mock_notify_lambda.invoke.assert_called_once()
        payload = json.loads(self.mock_notify_lambda.invoke.call_args[1]["Payload"])
        self.assertIn("Manual Review", payload["subject"])

    # ── 400 Validation Failures ───────────────────────────────────────────────

    @patch("Lambda_func.parse_onboarding_request")
    def test_400_missing_required_fields(self, mock_parse):
        mock_parse.return_value = {
            "username": None, "name": "Mystery Person",
            "Role": None, "Department": None,
        }
        resp = Lambda_func.lambda_handler(_event({"request": "some vague text"}), {})
        self.assertEqual(resp["statusCode"], 400)
        self.assertIn("Missing required", json.loads(resp["body"])["message"])

    @patch("Lambda_func.parse_onboarding_request")
    def test_400_ldap_injection_blocked(self, mock_parse):
        """Username with LDAP special chars must be rejected before any LDAP call."""
        mock_parse.return_value = {
            "username": "jdoe,CN=Admins",
            "name": "John Doe",
            "Role": "Software Engineer",
            "Department": "Engineering",
        }
        resp = Lambda_func.lambda_handler(_event({"request": "malicious input"}), {})
        self.assertEqual(resp["statusCode"], 400)
        self.assertIn("not permitted", json.loads(resp["body"])["message"])

    @patch("Lambda_func.parse_onboarding_request")
    def test_400_bedrock_parse_exception(self, mock_parse):
        mock_parse.side_effect = ValueError("Claude returned malformed JSON")
        resp = Lambda_func.lambda_handler(_event({"request": "gibberish"}), {})
        self.assertEqual(resp["statusCode"], 400)
        self.assertIn("Could not parse", json.loads(resp["body"])["message"])

    # ── URL-encoded body (Slack slash-command format) ─────────────────────────

    @patch("Lambda_func.log_onboarding_request")
    @patch("Lambda_func.get_confidence_threshold", return_value=0.8)
    @patch("Lambda_func.generate_notification", return_value="Carol onboarded.")
    @patch("Lambda_func.get_group_assignments")
    @patch("Lambda_func.parse_onboarding_request")
    def test_200_url_encoded_slack_body(
        self, mock_parse, mock_groups, mock_notify, mock_threshold, mock_log
    ):
        mock_parse.return_value = {
            "username": "cjones", "name": "Carol Jones",
            "Role": "HR Specialist", "Department": "Human Resources",
        }
        mock_groups.return_value = {
            "groups": ["Human Resources", "All Employees"],
            "confidence": 0.92,
            "reasoning": "HR Specialist maps directly.",
        }

        resp = Lambda_func.lambda_handler(
            {"body": "text=Please+onboard+Carol+Jones+as+HR+Specialist"}, {}
        )

        self.assertEqual(resp["statusCode"], 200)
        mock_parse.assert_called_once_with("Please onboard Carol Jones as HR Specialist")


if __name__ == "__main__":
    unittest.main()

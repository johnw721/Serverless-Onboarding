"""
Unit tests for Offboard_func.py (offboarding Lambda handler).

All external I/O is mocked:
  - boto3 clients replaced in setUp (same pattern as test_lambda_handler.py)
  - _parse_offboard_request, deprovision_from_entra, log_onboarding_request, and
    _send_sns_notification are patched on the Offboard_func module namespace

Patch targets use "Offboard_func.*" because Offboard_func imports symbols with
"from x import y", binding them to its own namespace.

USE_MOCK_LDAP=true is set before import so no real LDAP calls are attempted.
"""
import json
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

# --- Env setup BEFORE the module is imported ---
os.environ["AWS_DEFAULT_REGION"]       = "us-west-2"
os.environ["AWS_ACCESS_KEY_ID"]        = "testing"
os.environ["AWS_SECRET_ACCESS_KEY"]    = "testing"
os.environ["AWS_EC2_METADATA_DISABLED"] = "true"
os.environ["USE_MOCK_LDAP"]            = "true"
os.environ["AZURE_SYNC_ENABLED"]       = "false"
os.environ["DOMAIN"]                   = "business.abc.com"
os.environ["BASE_DN"]                  = "DC=business,DC=abc,DC=com"
os.environ["GROUP_BASE_DN"]            = "OU=Groups,DC=business,DC=abc,DC=com"
os.environ["NOTIFY_SNS_LAMBDA_NAME"]  = "notify_sns_function"
os.environ["SNS_TOPIC_ARN"]           = "arn:aws:sns:us-east-1:123456789012:it-notify"

import Offboard_func  # noqa: E402


def _event(body: str | dict) -> dict:
    """Build a Lambda event with the given body (dict auto-serialized to JSON)."""
    if isinstance(body, dict):
        body = json.dumps(body)
    return {"body": body}


class TestOffboardHandler(unittest.TestCase):

    def setUp(self):
        """Replace module-level boto3 clients so no real AWS calls fire."""
        Offboard_func.secretsmanager  = MagicMock()
        Offboard_func.notify_sns_lambda = MagicMock()

    # -----------------------------------------------------------------------
    # Happy path — 200
    # -----------------------------------------------------------------------

    @patch("Offboard_func.log_onboarding_request")
    @patch("Offboard_func.deprovision_from_entra", return_value=True)
    @patch("Offboard_func.generate_notification", return_value="Offboarded jdoe")
    @patch("Offboard_func._send_sns_notification")
    @patch("Offboard_func._parse_offboard_request",
           return_value={"username": "jdoe", "name": "Jane Doe", "confidence": 1.0})
    def test_200_happy_path(self, mock_parse, mock_sns, mock_notify, mock_deprov, mock_log):
        """Full offboarding with mock LDAP returns 200 with expected body keys."""
        resp = Offboard_func.lambda_handler(
            _event({"request": "Please offboard Jane Doe"}), {}
        )
        self.assertEqual(resp["statusCode"], 200)
        body = json.loads(resp["body"])
        self.assertIn("message", body)
        self.assertIn("account_disabled", body)
        self.assertIn("groups_removed", body)
        self.assertNotIn("azure_sync_warning", body)
        mock_log.assert_called_once()
        mock_deprov.assert_called_once_with("jdoe")

    # -----------------------------------------------------------------------
    # 200 with Entra ID warning in response body
    # -----------------------------------------------------------------------

    @patch("Offboard_func.log_onboarding_request")
    @patch("Offboard_func.deprovision_from_entra", return_value=False)  # Azure fails
    @patch("Offboard_func.generate_notification", return_value="Offboarded jdoe")
    @patch("Offboard_func._send_sns_notification")
    @patch("Offboard_func._parse_offboard_request",
           return_value={"username": "jdoe", "name": "Jane Doe", "confidence": 1.0})
    def test_200_with_azure_warning(self, mock_parse, mock_sns, mock_notify, mock_deprov, mock_log):
        """Entra ID failure is non-fatal; 200 is returned with azure_sync_warning in body."""
        resp = Offboard_func.lambda_handler(
            _event({"request": "Offboard Jane Doe"}), {}
        )
        self.assertEqual(resp["statusCode"], 200)
        body = json.loads(resp["body"])
        self.assertIn("azure_sync_warning", body)

    # -----------------------------------------------------------------------
    # 400 — Claude returns null username
    # -----------------------------------------------------------------------

    @patch("Offboard_func._parse_offboard_request",
           return_value={"username": None, "name": None})
    def test_400_missing_username(self, mock_parse):
        """If Claude cannot determine a username, return 400."""
        resp = Offboard_func.lambda_handler(
            _event({"request": "offboard someone"}), {}
        )
        self.assertEqual(resp["statusCode"], 400)
        body = json.loads(resp["body"])
        self.assertIn("username", body["message"].lower())

    # -----------------------------------------------------------------------
    # 400 — LDAP injection attempt
    # -----------------------------------------------------------------------

    @patch("Offboard_func._parse_offboard_request",
           return_value={"username": "jdoe)(|(cn=*)", "name": "Bad Actor"})
    def test_400_ldap_injection_blocked(self, mock_parse):
        """A username with LDAP special characters is rejected by sanitize_dn_value."""
        resp = Offboard_func.lambda_handler(
            _event({"request": "offboard jdoe)(|(cn=*)"}), {}
        )
        self.assertEqual(resp["statusCode"], 400)
        body = json.loads(resp["body"])
        self.assertIn("not permitted", body["message"].lower())

    # -----------------------------------------------------------------------
    # 400 — Bedrock parse exception
    # -----------------------------------------------------------------------

    @patch("Offboard_func._parse_offboard_request",
           side_effect=Exception("Bedrock timeout"))
    def test_400_bedrock_parse_exception(self, mock_parse):
        """Exception during Claude parsing returns 400 with error detail."""
        resp = Offboard_func.lambda_handler(
            _event({"request": "offboard Jane"}), {}
        )
        self.assertEqual(resp["statusCode"], 400)
        body = json.loads(resp["body"])
        self.assertIn("parse", body["message"].lower())

    # -----------------------------------------------------------------------
    # 500 — LDAP failure
    # -----------------------------------------------------------------------

    @patch("Offboard_func.log_onboarding_request")
    @patch("Offboard_func._send_sns_notification")
    @patch("Offboard_func._offboard_mock_ldap",
           side_effect=Exception("LDAP connection refused"))
    @patch("Offboard_func._parse_offboard_request",
           return_value={"username": "jdoe", "name": "Jane Doe", "confidence": 1.0})
    def test_500_ldap_failure(self, mock_parse, mock_ldap, mock_sns, mock_log):
        """Exception from LDAP offboarding returns 500 and logs the failure."""
        resp = Offboard_func.lambda_handler(
            _event({"request": "offboard Jane Doe"}), {}
        )
        self.assertEqual(resp["statusCode"], 500)
        body = json.loads(resp["body"])
        self.assertIn("failed", body["message"].lower())
        # Failure is logged to DynamoDB
        mock_log.assert_called_once()
        # IT is notified even on failure
        mock_sns.assert_called_once()

    # -----------------------------------------------------------------------
    # 202 — low confidence (ambiguous identity)
    # -----------------------------------------------------------------------

    @patch("Offboard_func.log_onboarding_request")
    @patch("Offboard_func._send_sns_notification")
    @patch("Offboard_func.get_offboard_confidence_threshold", return_value=0.95)
    @patch("Offboard_func._parse_offboard_request",
           return_value={"username": "jsmith", "name": "John", "confidence": 0.70})
    def test_202_low_confidence_held_for_review(self, mock_parse, mock_thresh, mock_sns, mock_log):
        """Confidence below threshold returns 202 and sends SNS alert without touching LDAP."""
        resp = Offboard_func.lambda_handler(
            _event({"request": "offboard John"}), {}
        )
        self.assertEqual(resp["statusCode"], 202)
        body = json.loads(resp["body"])
        self.assertIn("manual review", body["message"].lower())
        self.assertIn("confidence", body)
        self.assertAlmostEqual(body["confidence"], 0.70)
        mock_log.assert_called_once()
        mock_sns.assert_called_once()

    @patch("Offboard_func.log_onboarding_request")
    @patch("Offboard_func.deprovision_from_entra", return_value=True)
    @patch("Offboard_func.generate_notification", return_value="done")
    @patch("Offboard_func._send_sns_notification")
    @patch("Offboard_func.get_offboard_confidence_threshold", return_value=0.95)
    @patch("Offboard_func._parse_offboard_request",
           return_value={"username": "jsmith", "name": "John Smith", "confidence": 0.95})
    def test_200_at_threshold_boundary(self, mock_parse, mock_thresh, mock_sns,
                                       mock_notify, mock_deprov, mock_log):
        """Confidence exactly at threshold (0.95) proceeds to offboard (not held)."""
        resp = Offboard_func.lambda_handler(
            _event({"request": "offboard John Smith"}), {}
        )
        self.assertEqual(resp["statusCode"], 200)

    # -----------------------------------------------------------------------
    # URL-encoded Slack-style body
    # -----------------------------------------------------------------------

    @patch("Offboard_func.log_onboarding_request")
    @patch("Offboard_func.deprovision_from_entra", return_value=True)
    @patch("Offboard_func.generate_notification", return_value="done")
    @patch("Offboard_func._send_sns_notification")
    @patch("Offboard_func._parse_offboard_request",
           return_value={"username": "jsmith", "name": "John Smith", "confidence": 1.0})
    def test_200_url_encoded_slack_body(self, mock_parse, mock_sns, mock_notify,
                                        mock_deprov, mock_log):
        """Handler correctly parses a URL-encoded Slack slash-command body."""
        resp = Offboard_func.lambda_handler(
            _event("text=Please+offboard+John+Smith"), {}
        )
        self.assertEqual(resp["statusCode"], 200)

    # -----------------------------------------------------------------------
    # Partial offboard (some group removals fail)
    # -----------------------------------------------------------------------

    @patch("Offboard_func.log_onboarding_request")
    @patch("Offboard_func.deprovision_from_entra", return_value=True)
    @patch("Offboard_func.generate_notification", return_value="Partial offboard")
    @patch("Offboard_func._send_sns_notification")
    @patch("Offboard_func._offboard_mock_ldap",
           return_value={"disabled": True, "groups_removed": ["Eng"], "groups_failed": ["VPN"]})
    @patch("Offboard_func._parse_offboard_request",
           return_value={"username": "jdoe", "name": "Jane Doe", "confidence": 1.0})
    def test_200_partial_offboard_includes_groups_failed(
            self, mock_parse, mock_ldap, mock_sns, mock_notify, mock_deprov, mock_log):
        """If any group removal fails, the response body includes groups_failed."""
        resp = Offboard_func.lambda_handler(
            _event({"request": "offboard Jane Doe"}), {}
        )
        self.assertEqual(resp["statusCode"], 200)
        body = json.loads(resp["body"])
        self.assertIn("groups_failed", body)
        self.assertEqual(body["groups_failed"], ["VPN"])
        self.assertIn("warning", body["message"].lower())


if __name__ == "__main__":
    unittest.m
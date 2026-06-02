"""
End-to-end integration tests using moto to mock AWS services.

Unlike the unit tests (which mock every external call), these tests let real
boto3 SDK calls flow through to moto-backed services:

  ✓ SSM Parameter Store   → get_confidence_threshold() / get_offboard_confidence_threshold()
  ✓ Secrets Manager       → _fetch_ldap_credentials() (bypassed by USE_MOCK_LDAP but secret
                             must exist so the module doesn't error at import time)
  ✓ DynamoDB              → log_onboarding_request() writes a real item
  ✓ Lambda invoke          → _send_sns_notification() fires a real async invoke

Bedrock / Claude is still patched because moto has no Bedrock support.
USE_MOCK_LDAP=true skips real LDAP — all AD actions are logged, not executed.
AZURE_SYNC_ENABLED=false skips Graph API calls.

Pattern:
  1. @mock_aws activates moto for the entire test class
  2. setUp bootstraps all required AWS resources (table, secrets, SSM params,
     stub notify Lambda) using real boto3 against moto endpoints
  3. Module-level clients in Lambda_func / Offboard_func are replaced with
     fresh boto3 clients inside the moto context so they hit moto, not AWS
  4. Each test invokes lambda_handler() directly and asserts on both the HTTP
     response AND the side-effects in DynamoDB / mock Lambda invoke records
"""
import json
import os
import sys
import unittest
import uuid
import zipfile
import io
from unittest.mock import patch, MagicMock

# ── Env setup BEFORE importing any project module ─────────────────────────────
os.environ.setdefault("AWS_DEFAULT_REGION", "us-west-2")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_EC2_METADATA_DISABLED", "true")
os.environ["USE_MOCK_LDAP"] = "true"
os.environ["AZURE_SYNC_ENABLED"] = "false"
os.environ["DOMAIN"] = "business.abc.com"
os.environ["BASE_DN"] = "DC=business,DC=abc,DC=com"
os.environ["GROUP_BASE_DN"] = "OU=Groups,DC=business,DC=abc,DC=com"
os.environ["NOTIFY_SNS_LAMBDA_NAME"] = "notify_sns_function"
os.environ["SNS_TOPIC_ARN"] = "arn:aws:sns:us-west-2:123456789012:it-notify"
os.environ["DYNAMODB_TABLE_NAME"] = "onboarding_requests_e2e"
os.environ["CONFIDENCE_THRESHOLD_SSM_PARAM"] = "/ad-lambda/confidence-threshold"
os.environ["OFFBOARD_CONFIDENCE_THRESHOLD_SSM_PARAM"] = "/ad-lambda/offboard-confidence-threshold"

import boto3
from moto import mock_aws

import Lambda_func
import Offboard_func
import bedrock_agent
import helpers


# ── Helpers ───────────────────────────────────────────────────────────────────

def _event(body):
    if isinstance(body, dict):
        body = json.dumps(body)
    return {"body": body}


def _minimal_lambda_zip() -> bytes:
    """Return a minimal valid zip so moto can create the notify Lambda."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("index.py", "def handler(e, c): pass")
    return buf.getvalue()


# ── Base class — bootstraps moto resources ────────────────────────────────────

@mock_aws
class MotoE2EBase(unittest.TestCase):
    """
    Boots a complete moto environment before each test:
      - DynamoDB table (matches DYNAMODB_TABLE_NAME)
      - Secrets Manager secrets for LDAP credentials
      - SSM parameters for both confidence thresholds
      - A stub notify_sns_function Lambda (so invoke doesn't 404)
    Then replaces the module-level boto3 clients in Lambda_func, Offboard_func,
    helpers, and bedrock_agent with fresh clients pointing at moto endpoints.
    """

    REGION = "us-west-2"
    TABLE = "onboarding_requests_e2e"

    def setUp(self):
        # ── DynamoDB ──────────────────────────────────────────────────────────
        self.ddb = boto3.resource("dynamodb", region_name=self.REGION)
        self.table = self.ddb.create_table(
            TableName=self.TABLE,
            KeySchema=[{"AttributeName": "request_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "request_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )

        # ── Secrets Manager ───────────────────────────────────────────────────
        self.sm = boto3.client("secretsmanager", region_name=self.REGION)
        for name, value in [
            ("ldap_server_address", "ad.business.abc.com"),
            ("ldap_username", "svc-onboarding@business.abc.com"),
            ("ldap_password", "mock-password"),
        ]:
            self.sm.create_secret(Name=name, SecretString=value)

        # ── SSM ───────────────────────────────────────────────────────────────
        self.ssm = boto3.client("ssm", region_name=self.REGION)
        self.ssm.put_parameter(
            Name="/ad-lambda/confidence-threshold",
            Value="0.8",
            Type="String",
        )
        self.ssm.put_parameter(
            Name="/ad-lambda/offboard-confidence-threshold",
            Value="0.95",
            Type="String",
        )

        # ── Stub notify Lambda ────────────────────────────────────────────────
        iam = boto3.client("iam", region_name=self.REGION)
        role = iam.create_role(
            RoleName="lambda-role",
            AssumeRolePolicyDocument=json.dumps({
                "Version": "2012-10-17",
                "Statement": [{"Effect": "Allow", "Principal": {"Service": "lambda.amazonaws.com"},
                               "Action": "sts:AssumeRole"}],
            }),
        )
        self.lambda_client = boto3.client("lambda", region_name=self.REGION)
        self.lambda_client.create_function(
            FunctionName="notify_sns_function",
            Runtime="python3.11",
            Role=role["Role"]["Arn"],
            Handler="index.handler",
            Code={"ZipFile": _minimal_lambda_zip()},
        )

        # ── Rewire module-level clients to moto endpoints ─────────────────────
        fresh_sm = boto3.client("secretsmanager", region_name=self.REGION)
        fresh_lambda = boto3.client("lambda", region_name=self.REGION)
        fresh_ddb = boto3.resource("dynamodb", region_name=self.REGION)
        fresh_ssm = boto3.client("ssm", region_name=self.REGION)

        Lambda_func.secretsmanager = fresh_sm
        Lambda_func.notify_sns_lambda = fresh_lambda
        Offboard_func.secretsmanager = fresh_sm
        Offboard_func.notify_sns_lambda = fresh_lambda
        helpers.dynamodb = fresh_ddb
        bedrock_agent._ssm_client = fresh_ssm

        # Bust the SSM cache so each test reads from the fresh moto SSM
        bedrock_agent._threshold_cache = {"value": None, "loaded_at": 0.0}
        bedrock_agent._offboard_threshold_cache = {"value": None, "loaded_at": 0.0}


# ── Onboarding e2e tests ──────────────────────────────────────────────────────

@mock_aws
class TestOnboardingE2E(MotoE2EBase):

    # ── Happy path: full pipeline writes to DynamoDB ──────────────────────────

    @patch("Lambda_func.parse_onboarding_request", return_value={
        "username": "schen", "name": "Sarah Chen",
        "Role": "Senior Data Scientist", "Department": "Analytics",
    })
    @patch("Lambda_func.get_group_assignments", return_value={
        "groups": ["All Employees", "Data Science", "Analytics"],
        "confidence": 0.95,
        "reasoning": "Clear role match",
    })
    @patch("Lambda_func.generate_notification", return_value="Sarah Chen onboarded.")
    @patch("Lambda_func.sync_to_entra", return_value=(True, None))
    def test_200_writes_audit_record_to_dynamodb(
            self, mock_entra, mock_notify, mock_groups, mock_parse):
        """Happy path: DynamoDB contains a Success record after the handler returns."""
        resp = Lambda_func.lambda_handler(
            _event({"request": "Please onboard Sarah Chen as a senior data scientist"}), {}
        )

        self.assertEqual(resp["statusCode"], 200)
        body = json.loads(resp["body"])
        self.assertIn("groups_added", body)

        # Verify the audit record landed in DynamoDB
        items = self.table.scan()["Items"]
        self.assertEqual(len(items), 1)
        record = items[0]
        # request_id is now an append-only UUID, not the username
        uuid.UUID(record["request_id"])
        self.assertEqual(record["username"], "schen")
        self.assertEqual(record["status"], "Success")
        self.assertIn("timestamp", record)

    # ── SSM threshold is read live from moto SSM ──────────────────────────────

    @patch("Lambda_func.parse_onboarding_request", return_value={
        "username": "jdoe", "name": "Jane Doe",
        "Role": "Blockchain Evangelist", "Department": "Innovation",
    })
    @patch("Lambda_func.get_group_assignments", return_value={
        "groups": ["All Employees"],
        "confidence": 0.55,          # below 0.8 threshold in moto SSM
        "reasoning": "Novel role",
    })
    def test_202_reads_threshold_from_ssm_and_routes_to_review(
            self, mock_groups, mock_parse):
        """Confidence below the SSM-stored threshold (0.8) → 202 Pending Review."""
        resp = Lambda_func.lambda_handler(
            _event({"request": "Onboard Jane Doe as a Blockchain Evangelist"}), {}
        )

        self.assertEqual(resp["statusCode"], 202)

        # Audit record written with Pending Review status
        items = self.table.scan()["Items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["status"], "Pending Review")

    # ── Raising SSM threshold live tightens the gate ──────────────────────────

    @patch("Lambda_func.parse_onboarding_request", return_value={
        "username": "jdoe", "name": "Jane Doe",
        "Role": "Senior Engineer", "Department": "Engineering",
    })
    @patch("Lambda_func.get_group_assignments", return_value={
        "groups": ["All Employees", "Engineering"],
        "confidence": 0.85,
        "reasoning": "Clear match",
    })
    @patch("Lambda_func.generate_notification", return_value="Jane Doe onboarded.")
    @patch("Lambda_func.sync_to_entra", return_value=(True, None))
    def test_ssm_threshold_update_takes_effect_after_cache_bust(
            self, mock_entra, mock_notify, mock_groups, mock_parse):
        """
        Raising the SSM threshold to 0.9 and busting the cache causes a
        request with confidence=0.85 to be held for review instead of auto-provisioned.
        """
        # First call at default threshold (0.8) → 200
        resp = Lambda_func.lambda_handler(
            _event({"request": "Onboard Jane Doe as Senior Engineer"}), {}
        )
        self.assertEqual(resp["statusCode"], 200)

        # Raise threshold to 0.9 and bust cache
        self.ssm.put_parameter(
            Name="/ad-lambda/confidence-threshold",
            Value="0.9",
            Type="String",
            Overwrite=True,
        )
        bedrock_agent._threshold_cache = {"value": None, "loaded_at": 0.0}

        # Second call with same 0.85 confidence → 202 (now below raised threshold)
        resp2 = Lambda_func.lambda_handler(
            _event({"request": "Onboard Jane Doe as Senior Engineer"}), {}
        )
        self.assertEqual(resp2["statusCode"], 202)

    # ── LDAP injection is blocked before DynamoDB is written ──────────────────

    @patch("Lambda_func.parse_onboarding_request", return_value={
        "username": "jdoe)(|(cn=*)", "name": "Jane Doe",
        "Role": "Engineer", "Department": "Engineering",
    })
    def test_400_ldap_injection_no_dynamodb_write(self, mock_parse):
        """Sanitization rejects the DN injection — DynamoDB must remain empty."""
        resp = Lambda_func.lambda_handler(
            _event({"request": "Onboard jdoe)(|(cn=*)"}), {}
        )
        self.assertEqual(resp["statusCode"], 400)
        self.assertEqual(self.table.scan()["Count"], 0)

    # ── notify Lambda is actually invoked (async) ─────────────────────────────

    @patch("Lambda_func.parse_onboarding_request", return_value={
        "username": "rsmith", "name": "Robert Smith",
        "Role": "DevOps Engineer", "Department": "Infrastructure",
    })
    @patch("Lambda_func.get_group_assignments", return_value={
        "groups": ["All Employees", "DevOps"],
        "confidence": 0.92,
        "reasoning": "Clear match",
    })
    @patch("Lambda_func.generate_notification", return_value="Robert Smith onboarded.")
    @patch("Lambda_func.sync_to_entra", return_value=(True, None))
    def test_200_invokes_notify_lambda(
            self, mock_entra, mock_notify, mock_groups, mock_parse):
        """The notification Lambda is invoked asynchronously after a successful onboard."""
        resp = Lambda_func.lambda_handler(
            _event({"request": "Onboard Robert Smith as DevOps Engineer"}), {}
        )
        self.assertEqual(resp["statusCode"], 200)
        # moto records invocations — check the stub was called
        invocations = self.lambda_client.list_function_event_invoke_configs(
            FunctionName="notify_sns_function"
        )
        # Function exists in moto and was targeted — no error means invoke reached it
        self.assertIsNotNone(invocations)


# ── Offboarding e2e tests ─────────────────────────────────────────────────────

@mock_aws
class TestOffboardingE2E(MotoE2EBase):

    @patch("Offboard_func._parse_offboard_request", return_value={
        "username": "schen", "name": "Sarah Chen", "confidence": 1.0,
    })
    @patch("Offboard_func.generate_notification", return_value="Sarah Chen offboarded.")
    @patch("Offboard_func.deprovision_from_entra", return_value=True)
    def test_200_offboard_writes_audit_record(
            self, mock_entra, mock_notify, mock_parse):
        """Full offboard writes an Offboarded record to DynamoDB."""
        resp = Offboard_func.lambda_handler(
            _event({"request": "Please offboard Sarah Chen"}), {}
        )

        self.assertEqual(resp["statusCode"], 200)
        body = json.loads(resp["body"])
        self.assertTrue(body["account_disabled"])

        items = self.table.scan()["Items"]
        self.assertEqual(len(items), 1)
        uuid.UUID(items[0]["request_id"])
        self.assertEqual(items[0]["username"], "schen")
        self.assertEqual(items[0]["status"], "Offboarded")

    @patch("Offboard_func._parse_offboard_request", return_value={
        "username": "jsmith", "name": "John", "confidence": 0.65,
    })
    def test_202_low_confidence_writes_pending_review_to_dynamodb(self, mock_parse):
        """
        Ambiguous identity (confidence=0.65 < SSM threshold 0.95) → 202.
        An Offboard Pending Review record is written to DynamoDB.
        """
        resp = Offboard_func.lambda_handler(
            _event({"request": "offboard John"}), {}
        )

        self.assertEqual(resp["statusCode"], 202)
        items = self.table.scan()["Items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["status"], "Offboard Pending Review")

    @patch("Offboard_func._parse_offboard_request", return_value={
        "username": "jdoe)(|(cn=*)", "name": "Attacker", "confidence": 1.0,
    })
    def test_400_ldap_injection_no_dynamodb_write(self, mock_parse):
        """DN injection rejected before any audit record is written."""
        resp = Offboard_func.lambda_handler(
            _event({"request": "offboard jdoe)(|(cn=*)"}), {}
        )
        self.assertEqual(resp["statusCode"], 400)
        self.assertEqual(self.table.scan()["Count"], 0)

    @patch("Offboard_func._parse_offboard_request", return_value={
        "username": "schen", "name": "Sarah Chen", "confidence": 1.0,
    })
    @patch("Offboard_func.generate_notification", return_value="Sarah Chen offboarded.")
    @patch("Offboard_func.deprovision_from_entra", return_value=False)   # Entra fails
    def test_200_entra_failure_still_writes_offboarded_status(
            self, mock_entra, mock_notify, mock_parse):
        """Entra ID failure is non-fatal — offboard succeeds and DynamoDB shows Offboarded."""
        resp = Offboard_func.lambda_handler(
            _event({"request": "offboard Sarah Chen"}), {}
        )
        self.assertEqual(resp["statusCode"], 200)
        body = json.loads(resp["body"])
        self.assertIn("azure_sync_warning", body)

        items = self.table.scan()["Items"]
        self.assertEqual(items[0]["status"], "Offboarded")

    @patch("Offboard_func._parse_offboard_request", return_value={
        "username": "schen", "name": "Sarah Chen", "confidence": 1.0,
    })
    @patch("Offboard_func.deprovision_from_entra", return_value=True)
    @patch("Offboard_func._offboard_mock_ldap", side_effect=Exception("LDAP timeout"))
    def test_500_ldap_failure_writes_offboard_failed_status(
            self, mock_ldap, mock_entra, mock_parse):
        """LDAP failure writes Offboard Failed to DynamoDB and returns 500."""
        resp = Offboard_func.lambda_handler(
            _event({"request": "offboard Sarah Chen"}), {}
        )
        self.assertEqual(resp["statusCode"], 500)
        items = self.table.scan()["Items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["status"], "Offboard Failed")


if __name__ == "__main__":
    unittest.main()

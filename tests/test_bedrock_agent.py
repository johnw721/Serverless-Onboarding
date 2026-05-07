"""
Unit tests for bedrock_agent.py — all Bedrock calls are mocked.
Run from the project root: python -m pytest tests/
"""
import sys
import os
import json
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'lambda-package'))


def _mock_bedrock_response(payload: dict) -> dict:
    """Build a fake boto3 invoke_model response wrapping the given payload."""
    body_bytes = json.dumps({
        "content": [{"text": json.dumps(payload)}]
    }).encode()
    return {"body": MagicMock(read=MagicMock(return_value=body_bytes))}


def _mock_bedrock_response_raw(text: str) -> dict:
    """Build a fake response where Claude returns arbitrary text (e.g. with fences)."""
    body_bytes = json.dumps({
        "content": [{"text": text}]
    }).encode()
    return {"body": MagicMock(read=MagicMock(return_value=body_bytes))}


FALLBACK_MAP = {
    "Software Engineer": ["Engineering", "All Employees"],
    "Data Scientist": ["Data Science", "All Employees"],
    "Product Manager": ["Product", "All Employees"],
}


class TestParseOnboardingRequest(unittest.TestCase):

    @patch("bedrock_agent._get_bedrock_client")
    def test_parses_standard_request(self, mock_get_client):
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client
        mock_client.invoke_model.return_value = _mock_bedrock_response({
            "username": "schen",
            "name": "Sarah Chen",
            "Role": "Data Scientist",
            "Department": "Data Science",
        })

        import bedrock_agent
        result = bedrock_agent.parse_onboarding_request(
            "Please onboard Sarah Chen as a data scientist starting Monday"
        )

        self.assertEqual(result["username"], "schen")
        self.assertEqual(result["name"], "Sarah Chen")
        self.assertEqual(result["Role"], "Data Scientist")
        self.assertEqual(result["Department"], "Data Science")

    @patch("bedrock_agent._get_bedrock_client")
    def test_strips_markdown_fences(self, mock_get_client):
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client
        fenced = '```json\n{"username": "jdoe", "name": "John Doe", "Role": "Engineer", "Department": "Engineering"}\n```'
        mock_client.invoke_model.return_value = _mock_bedrock_response_raw(fenced)

        import bedrock_agent
        result = bedrock_agent.parse_onboarding_request("Onboard John Doe as Engineer")
        self.assertEqual(result["username"], "jdoe")

    @patch("bedrock_agent._get_bedrock_client")
    def test_missing_keys_default_to_none(self, mock_get_client):
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client
        # Claude returns only partial fields
        mock_client.invoke_model.return_value = _mock_bedrock_response({
            "username": "jdoe",
            "name": "John Doe",
        })

        import bedrock_agent
        result = bedrock_agent.parse_onboarding_request("Onboard John Doe")
        self.assertIsNone(result["Role"])
        self.assertIsNone(result["Department"])


class TestGetGroupAssignments(unittest.TestCase):

    @patch("bedrock_agent._get_bedrock_client")
    def test_high_confidence_assignment(self, mock_get_client):
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client
        mock_client.invoke_model.return_value = _mock_bedrock_response({
            "groups": ["Engineering", "All Employees"],
            "confidence": 0.95,
            "reasoning": "Clear match to Software Engineer.",
        })

        import bedrock_agent
        result = bedrock_agent.get_group_assignments(
            {"Role": "Software Engineer", "Department": "Engineering"},
            FALLBACK_MAP,
        )

        self.assertGreaterEqual(result["confidence"], 0.8)
        self.assertIn("Engineering", result["groups"])
        self.assertIn("All Employees", result["groups"])

    @patch("bedrock_agent._get_bedrock_client")
    def test_always_injects_all_employees(self, mock_get_client):
        """Claude forgot to include All Employees — the code should add it."""
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client
        mock_client.invoke_model.return_value = _mock_bedrock_response({
            "groups": ["Engineering"],
            "confidence": 0.9,
            "reasoning": "Test.",
        })

        import bedrock_agent
        result = bedrock_agent.get_group_assignments(
            {"Role": "Software Engineer", "Department": "Engineering"},
            FALLBACK_MAP,
        )
        self.assertIn("All Employees", result["groups"])

    @patch("bedrock_agent._get_bedrock_client")
    def test_falls_back_to_hardcoded_map_on_bedrock_error(self, mock_get_client):
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client
        mock_client.invoke_model.side_effect = Exception("Bedrock unavailable")

        import bedrock_agent
        result = bedrock_agent.get_group_assignments(
            {"Role": "Software Engineer", "Department": "Engineering"},
            FALLBACK_MAP,
        )

        self.assertEqual(result["confidence"], 1.0)
        self.assertIn("Engineering", result["groups"])
        self.assertIn("All Employees", result["groups"])

    @patch("bedrock_agent._get_bedrock_client")
    def test_unknown_role_fallback_returns_low_confidence(self, mock_get_client):
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client
        mock_client.invoke_model.side_effect = Exception("Bedrock unavailable")

        import bedrock_agent
        result = bedrock_agent.get_group_assignments(
            {"Role": "Chief Vibes Officer", "Department": "Culture"},
            FALLBACK_MAP,
        )

        self.assertEqual(result["confidence"], 0.0)
        self.assertEqual(result["groups"], ["All Employees"])

    @patch("bedrock_agent._get_bedrock_client")
    def test_low_confidence_triggers_202_path(self, mock_get_client):
        """Confidence below threshold should be detectable by the handler."""
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client
        mock_client.invoke_model.return_value = _mock_bedrock_response({
            "groups": ["All Employees"],
            "confidence": 0.45,
            "reasoning": "Role is too ambiguous to map with confidence.",
        })

        import bedrock_agent
        result = bedrock_agent.get_group_assignments(
            {"Role": "Innovation Catalyst", "Department": "Strategy"},
            FALLBACK_MAP,
        )

        self.assertLess(result["confidence"], bedrock_agent.get_confidence_threshold())


class TestGenerateNotification(unittest.TestCase):

    @patch("bedrock_agent._get_bedrock_client")
    def test_returns_claude_text_on_success(self, mock_get_client):
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client
        body_bytes = json.dumps({
            "content": [{"text": "Sarah Chen has been successfully onboarded as Data Scientist."}]
        }).encode()
        mock_client.invoke_model.return_value = {
            "body": MagicMock(read=MagicMock(return_value=body_bytes))
        }

        import bedrock_agent
        msg = bedrock_agent.generate_notification(
            {"name": "Sarah Chen", "username": "schen", "Role": "Data Scientist", "Department": "Data Science"},
            ["Data Science", "All Employees"],
            "Success",
        )
        self.assertIn("Sarah Chen", msg)

    @patch("bedrock_agent._get_bedrock_client")
    def test_falls_back_to_template_on_bedrock_error(self, mock_get_client):
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client
        mock_client.invoke_model.side_effect = Exception("timeout")

        import bedrock_agent
        msg = bedrock_agent.generate_notification(
            {"name": "John Doe", "username": "jdoe", "Role": "Engineer", "Department": "Engineering"},
            ["Engineering", "All Employees"],
            "Failed",
        )
        self.assertIn("John Doe", msg)
        self.assertIn("Manual intervention", msg)


if __name__ == "__main__":
    unittest.main()

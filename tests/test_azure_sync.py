"""
Unit tests for azure_sync.py.

All network I/O (urllib.request.urlopen) and AWS calls (Secrets Manager)
are mocked so these tests run in CI with no real credentials or network access.

Patch strategy:
  - azure_sync._sm_client is assigned directly (lazy-init via global var)
  - urllib.request.urlopen is patched at the source module so every call
    made by azure_sync hits the mock
  - azure_sync.AZURE_SYNC_ENABLED is patched per-test for the sync-disabled cases
"""
import json
import os
import sys
import time
import unittest
from io import BytesIO
from unittest.mock import MagicMock, patch, call

# --- Env setup before import so no real AWS calls fire at module level ---
os.environ.setdefault("AWS_DEFAULT_REGION", "us-west-2")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_EC2_METADATA_DISABLED", "true")
os.environ.setdefault("DOMAIN", "business.abc.com")
# Default to disabled; individual tests patch AZURE_SYNC_ENABLED as needed
os.environ.setdefault("AZURE_SYNC_ENABLED", "false")

import azure_sync  # noqa: E402 — env vars must be set first


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_urlopen_response(body: dict, status: int = 200):
    """
    Return a context-manager mock whose .read() returns JSON-encoded bytes.
    Mimics the object returned by urllib.request.urlopen(req).
    """
    raw = json.dumps(body).encode()
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=cm)
    cm.__exit__ = MagicMock(return_value=False)
    cm.read = MagicMock(return_value=raw)
    cm.status = status
    return cm


def _make_sm_client(tenant="tid", client_id="cid", secret="csec"):
    """Return a mock Secrets Manager client that returns preset credentials."""
    sm = MagicMock()
    def _get(SecretId):
        mapping = {
            "azure_tenant_id":     {"SecretString": tenant},
            "azure_client_id":     {"SecretString": client_id},
            "azure_client_secret": {"SecretString": secret},
        }
        return mapping[SecretId]
    sm.get_secret_value.side_effect = _get
    return sm


# ---------------------------------------------------------------------------
# Test class
# ---------------------------------------------------------------------------

class TestAzureSync(unittest.TestCase):

    def setUp(self):
        """Reset all module-level caches before each test."""
        azure_sync._creds = {"tenant_id": None, "client_id": None, "client_secret": None}
        azure_sync._token_cache = {"token": None, "expires_at": 0.0}
        azure_sync._sm_client = None

    # -----------------------------------------------------------------------
    # sync_to_entra — disabled path
    # -----------------------------------------------------------------------

    def test_sync_disabled_returns_true_none(self):
        """When AZURE_SYNC_ENABLED is False, sync_to_entra returns (True, None)."""
        with patch.object(azure_sync, "AZURE_SYNC_ENABLED", False):
            ok, pw = azure_sync.sync_to_entra({"username": "jdoe", "name": "Jane Doe"})
        self.assertTrue(ok)
        self.assertIsNone(pw)

    # -----------------------------------------------------------------------
    # sync_to_entra — Secrets Manager failure
    # -----------------------------------------------------------------------

    def test_sync_sm_failure_returns_false_none(self):
        """SM exception during credential load → (False, None); no network calls."""
        from botocore.exceptions import ClientError

        bad_sm = MagicMock()
        bad_sm.get_secret_value.side_effect = ClientError(
            {"Error": {"Code": "ResourceNotFoundException", "Message": "nope"}},
            "GetSecretValue",
        )
        azure_sync._sm_client = bad_sm

        with patch.object(azure_sync, "AZURE_SYNC_ENABLED", True), \
             patch("azure_sync.urllib.request.urlopen") as mock_urlopen:
            ok, pw = azure_sync.sync_to_entra({"username": "jdoe", "name": "Jane Doe"})

        self.assertFalse(ok)
        self.assertIsNone(pw)
        mock_urlopen.assert_not_called()

    # -----------------------------------------------------------------------
    # sync_to_entra — token fetch failure
    # -----------------------------------------------------------------------

    def test_sync_token_failure_returns_false_none(self):
        """If the OAuth token request fails, sync_to_entra returns (False, None)."""
        import urllib.error

        azure_sync._sm_client = _make_sm_client()

        with patch.object(azure_sync, "AZURE_SYNC_ENABLED", True), \
             patch("azure_sync.urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = urllib.error.URLError("connection refused")
            ok, pw = azure_sync.sync_to_entra({"username": "jdoe", "name": "Jane Doe"})

        self.assertFalse(ok)
        self.assertIsNone(pw)

    # -----------------------------------------------------------------------
    # sync_to_entra — happy path
    # -----------------------------------------------------------------------

    def test_sync_happy_path_returns_true_and_password(self):
        """Successful provisioning returns (True, password) where password starts with 'T!'."""
        azure_sync._sm_client = _make_sm_client()

        token_resp = _make_urlopen_response({"access_token": "tok123", "expires_in": 3600})
        graph_resp = _make_urlopen_response({"id": "user-object-id"}, status=201)

        with patch.object(azure_sync, "AZURE_SYNC_ENABLED", True), \
             patch("azure_sync.urllib.request.urlopen", side_effect=[token_resp, graph_resp]):
            ok, pw = azure_sync.sync_to_entra({"username": "jdoe", "name": "Jane Doe"})

        self.assertTrue(ok)
        self.assertIsNotNone(pw)
        self.assertTrue(pw.startswith("T!"), f"Expected password to start with 'T!', got: {pw!r}")
        self.assertGreaterEqual(len(pw), 10)

    # -----------------------------------------------------------------------
    # sync_to_entra — Graph API failure
    # -----------------------------------------------------------------------

    def test_sync_graph_failure_returns_false_none(self):
        """A 400 from the Graph API (HTTPError) → (False, None)."""
        import urllib.error

        azure_sync._sm_client = _make_sm_client()

        token_resp = _make_urlopen_response({"access_token": "tok123", "expires_in": 3600})
        http_err = urllib.error.HTTPError(
            url="https://graph.microsoft.com/v1.0/users",
            code=400,
            msg="Bad Request",
            hdrs={},
            fp=BytesIO(b'{"error": {"code": "Request_BadRequest"}}'),
        )

        with patch.object(azure_sync, "AZURE_SYNC_ENABLED", True), \
             patch("azure_sync.urllib.request.urlopen", side_effect=[token_resp, http_err]):
            ok, pw = azure_sync.sync_to_entra({"username": "jdoe", "name": "Jane Doe"})

        self.assertFalse(ok)
        self.assertIsNone(pw)

    # -----------------------------------------------------------------------
    # deprovision_from_entra — disabled path
    # -----------------------------------------------------------------------

    def test_deprovision_disabled_returns_true(self):
        """When sync is disabled, deprovision_from_entra returns True immediately."""
        with patch.object(azure_sync, "AZURE_SYNC_ENABLED", False):
            result = azure_sync.deprovision_from_entra("jdoe")
        self.assertTrue(result)

    # -----------------------------------------------------------------------
    # deprovision_from_entra — happy path (3 urlopen calls: token, GET, PATCH)
    # -----------------------------------------------------------------------

    def test_deprovision_happy_path(self):
        """Successful deprovisioning: token fetch → GET user → PATCH disable → True."""
        azure_sync._sm_client = _make_sm_client()

        token_resp = _make_urlopen_response({"access_token": "tok123", "expires_in": 3600})
        get_resp   = _make_urlopen_response({"id": "obj-999"})
        patch_resp = _make_urlopen_response({})  # PATCH returns empty body on 204

        with patch.object(azure_sync, "AZURE_SYNC_ENABLED", True), \
             patch("azure_sync.urllib.request.urlopen",
                   side_effect=[token_resp, get_resp, patch_resp]):
            result = azure_sync.deprovision_from_entra("jdoe")

        self.assertTrue(result)

    # -----------------------------------------------------------------------
    # deprovision_from_entra — user not found (404)
    # -----------------------------------------------------------------------

    def test_deprovision_user_not_found(self):
        """404 on GET user → deprovision_from_entra returns False."""
        import urllib.error

        azure_sync._sm_client = _make_sm_client()

        token_resp = _make_urlopen_response({"access_token": "tok123", "expires_in": 3600})
        not_found = urllib.error.HTTPError(
            url="https://graph.microsoft.com/v1.0/users/jdoe@business.abc.com",
            code=404,
            msg="Not Found",
            hdrs={},
            fp=BytesIO(b'{"error": {"code": "Request_ResourceNotFound"}}'),
        )

        with patch.object(azure_sync, "AZURE_SYNC_ENABLED", True), \
             patch("azure_sync.urllib.request.urlopen", side_effect=[token_resp, not_found]):
            result = azure_sync.deprovision_from_entra("jdoe")

        self.assertFalse(result)

    # -----------------------------------------------------------------------
    # Token cache re-use
    # -----------------------------------------------------------------------

    def test_token_is_cached_across_calls(self):
        """
        Second Graph call re-uses the cached token — urlopen is only called once
        for the token endpoint across two consecutive _graph_request calls.
        """
        azure_sync._sm_client = _make_sm_client()

        token_resp  = _make_urlopen_response({"access_token": "cached_tok", "expires_in": 3600})
        graph_resp1 = _make_urlopen_response({"id": "a"})
        graph_resp2 = _make_urlopen_response({})

        with patch.object(azure_sync, "AZURE_SYNC_ENABLED", True), \
             patch("azure_sync.urllib.request.urlopen",
                   side_effect=[token_resp, graph_resp1, graph_resp2]) as mock_open:
            azure_sync._graph_request("GET", "/users/jdoe@business.abc.com")
            azure_sync._graph_request("PATCH", "/users/obj-id", {"accountEnabled": False})

        # 3 calls: 1 token + 2 graph (not 4, proving cache hit)
        self.assertEqual(mock_open.call_count, 3)
        self.assertEqual(azure_sync._token_cache["token"], "cached_tok")

    # -----------------------------------------------------------------------
    # _generate_temp_password — randomness
    # -----------------------------------------------------------------------

    def test_generate_temp_password_is_random(self):
        """Two calls to _generate_temp_password should (almost certainly) differ."""
        pw1 = azure_sync._generate_temp_password()
        pw2 = azure_sync._generate_temp_password()
        self.assertNotEqual(pw1, pw2,
            "Two generated passwords were identical — randomness may be broken")

    def test_generate_temp_password_format(self):
        """Password starts with 'T!', followed by a digit, then alphanumeric chars."""
        import re
        for _ in range(20):  # run a few times to catch intermittent failures
            pw = azure_sync._generate_temp_password()
            self.assertTrue(pw.startswith("T!"),
                f"Password {pw!r} does not start with 'T!'")
            self.assertRegex(pw, r'^T!\d[a-zA-Z0-9]{16}$',
                f"Password {pw!r} does not match expected pattern T!<digit><16 alphanum>")


if __name__ == "__main__":
    unittest.main()

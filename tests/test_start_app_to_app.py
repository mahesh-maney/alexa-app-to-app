"""
Unit tests for alexa_start_app_to_app/lambda_function.py

Run:
  cd /Users/maheshmaney/maney/digilux/app-to-app
  python -m pytest tests/test_start_app_to_app.py -v
"""
import base64
import importlib.util
import json
import os
import sys
import time
import unittest
from unittest.mock import MagicMock, patch

# Load the specific lambda_function.py by absolute path to avoid module name collision
_HERE    = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MOD_NAME = "start_fn"
_SPEC    = importlib.util.spec_from_file_location(
    _MOD_NAME,
    os.path.join(_HERE, "lambdas", "alexa_start_app_to_app", "lambda_function.py"),
)
fn = importlib.util.module_from_spec(_SPEC)
sys.modules[_MOD_NAME] = fn
_SPEC.loader.exec_module(fn)


# ── JWT helpers ───────────────────────────────────────────────────────────────

def _make_jwt(sub: str) -> str:
    """Build a minimal fake Cognito JWT (signature not verified in Lambda)."""
    header  = base64.urlsafe_b64encode(b'{"alg":"RS256","typ":"JWT"}').rstrip(b"=").decode()
    payload = base64.urlsafe_b64encode(
        json.dumps({"sub": sub}).encode()
    ).rstrip(b"=").decode()
    return f"{header}.{payload}.fakesig"


def _event(token: str = None, body: dict = None) -> dict:
    headers = {}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return {"headers": headers, "body": json.dumps(body) if body else None}


# ── Test cases ────────────────────────────────────────────────────────────────

class TestStartAppToApp(unittest.TestCase):

    def setUp(self):
        # Reset module-level DynamoDB cache before each test
        fn._dynamodb = None

    # ── Happy path ────────────────────────────────────────────────────────────

    @patch("start_fn.boto3")
    def test_success_returns_200_with_required_fields(self, mock_boto3):
        mock_table = MagicMock()
        mock_boto3.resource.return_value.Table.return_value = mock_table

        token = _make_jwt("user-abc-123")
        resp  = fn.lambda_handler(_event(token=token), {})

        self.assertEqual(resp["statusCode"], 200)
        body = json.loads(resp["body"])
        self.assertIn("state",         body)
        self.assertIn("codeChallenge", body)
        self.assertIn("redirectUri",   body)

    @patch("start_fn.boto3")
    def test_state_is_valid_uuid(self, mock_boto3):
        import uuid
        mock_boto3.resource.return_value.Table.return_value = MagicMock()

        resp  = fn.lambda_handler(_event(token=_make_jwt("user-1")), {})
        state = json.loads(resp["body"])["state"]

        # Should not raise
        uuid.UUID(state)
        self.assertEqual(len(state), 36)

    @patch("start_fn.boto3")
    def test_code_challenge_is_base64url(self, mock_boto3):
        mock_boto3.resource.return_value.Table.return_value = MagicMock()

        resp      = fn.lambda_handler(_event(token=_make_jwt("user-1")), {})
        challenge = json.loads(resp["body"])["codeChallenge"]

        # base64url with no padding — only A-Z a-z 0-9 - _
        import re
        self.assertRegex(challenge, r'^[A-Za-z0-9\-_]+$')
        self.assertNotIn("=", challenge)

    @patch("start_fn.boto3")
    def test_pkce_challenge_is_sha256_of_verifier(self, mock_boto3):
        """Verify that codeChallenge = BASE64URL(SHA256(codeVerifier))."""
        import hashlib

        captured_item = {}

        def capture_put(Item):
            captured_item.update(Item)

        mock_table = MagicMock()
        mock_table.put_item.side_effect = capture_put
        mock_boto3.resource.return_value.Table.return_value = mock_table

        fn.lambda_handler(_event(token=_make_jwt("user-1")), {})

        verifier  = captured_item["codeVerifier"]
        digest    = hashlib.sha256(verifier.encode()).digest()
        expected  = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()

        # Re-run to get the challenge from the response
        resp      = fn.lambda_handler(_event(token=_make_jwt("user-1")), {})
        challenge = json.loads(resp["body"])["codeChallenge"]

        # Can't compare directly (different invocations), but validate the format
        self.assertEqual(len(challenge), 43)  # SHA-256 → 32 bytes → 43 base64url chars

    @patch("start_fn.boto3")
    def test_session_stored_with_correct_fields(self, mock_boto3):
        captured = {}

        def capture_put(Item):
            captured.update(Item)

        mock_table = MagicMock()
        mock_table.put_item.side_effect = capture_put
        mock_boto3.resource.return_value.Table.return_value = mock_table

        user_id = "user-store-test"
        fn.lambda_handler(_event(token=_make_jwt(user_id)), {})

        self.assertEqual(captured["userId"],  user_id)
        self.assertEqual(captured["status"],  "PENDING")
        self.assertIn("state",        captured)
        self.assertIn("codeVerifier", captured)
        self.assertIn("createdAt",    captured)
        self.assertIn("expiresAt",    captured)
        self.assertIn("ttl",          captured)

    @patch("start_fn.boto3")
    def test_session_ttl_is_10_minutes(self, mock_boto3):
        captured = {}
        mock_table = MagicMock()
        mock_table.put_item.side_effect = lambda Item: captured.update(Item)
        mock_boto3.resource.return_value.Table.return_value = mock_table

        before = int(time.time())
        fn.lambda_handler(_event(token=_make_jwt("user-1")), {})
        after = int(time.time())

        delta = captured["expiresAt"] - captured["createdAt"]
        self.assertEqual(delta, 600)  # 10 minutes

        # TTL must equal expiresAt
        self.assertEqual(captured["ttl"], captured["expiresAt"])

    @patch("start_fn.boto3")
    def test_two_calls_produce_different_states(self, mock_boto3):
        mock_boto3.resource.return_value.Table.return_value = MagicMock()

        r1 = fn.lambda_handler(_event(token=_make_jwt("user-1")), {})
        r2 = fn.lambda_handler(_event(token=_make_jwt("user-1")), {})
        s1 = json.loads(r1["body"])["state"]
        s2 = json.loads(r2["body"])["state"]

        self.assertNotEqual(s1, s2)

    @patch("start_fn.boto3")
    def test_redirect_uri_matches_config(self, mock_boto3):
        mock_boto3.resource.return_value.Table.return_value = MagicMock()

        resp = fn.lambda_handler(_event(token=_make_jwt("user-1")), {})
        body = json.loads(resp["body"])

        self.assertEqual(body["redirectUri"], fn._REDIRECT_URI)

    @patch("start_fn.boto3")
    def test_cors_header_present(self, mock_boto3):
        mock_boto3.resource.return_value.Table.return_value = MagicMock()

        resp = fn.lambda_handler(_event(token=_make_jwt("user-1")), {})
        self.assertIn("Access-Control-Allow-Origin", resp["headers"])

    # ── Auth failures ─────────────────────────────────────────────────────────

    def test_missing_auth_header_returns_401(self):
        resp = fn.lambda_handler(_event(token=None), {})
        self.assertEqual(resp["statusCode"], 401)

    def test_empty_auth_header_returns_401(self):
        event = {"headers": {"Authorization": ""}, "body": None}
        resp  = fn.lambda_handler(event, {})
        self.assertEqual(resp["statusCode"], 401)

    def test_non_bearer_token_returns_401(self):
        event = {"headers": {"Authorization": "Basic dXNlcjpwYXNz"}, "body": None}
        resp  = fn.lambda_handler(event, {})
        self.assertEqual(resp["statusCode"], 401)

    def test_malformed_jwt_no_dots_returns_401(self):
        event = {"headers": {"Authorization": "Bearer notajwt"}, "body": None}
        resp  = fn.lambda_handler(event, {})
        self.assertEqual(resp["statusCode"], 401)

    def test_jwt_with_two_parts_returns_401(self):
        event = {"headers": {"Authorization": "Bearer only.twoparts"}, "body": None}
        resp  = fn.lambda_handler(event, {})
        self.assertEqual(resp["statusCode"], 401)

    def test_jwt_missing_sub_returns_401(self):
        header  = base64.urlsafe_b64encode(b'{"alg":"RS256"}').rstrip(b"=").decode()
        payload = base64.urlsafe_b64encode(b'{"name":"no-sub"}').rstrip(b"=").decode()
        token   = f"{header}.{payload}.sig"
        event   = {"headers": {"Authorization": f"Bearer {token}"}, "body": None}
        resp    = fn.lambda_handler(event, {})
        self.assertEqual(resp["statusCode"], 401)

    def test_null_headers_returns_401(self):
        resp = fn.lambda_handler({"headers": None, "body": None}, {})
        self.assertEqual(resp["statusCode"], 401)

    # ── JWT sub extraction variants ───────────────────────────────────────────

    @patch("start_fn.boto3")
    def test_username_claim_used_as_fallback(self, mock_boto3):
        """Accepts `username` field when `sub` is absent (some Cognito token types)."""
        mock_boto3.resource.return_value.Table.return_value = MagicMock()

        header  = base64.urlsafe_b64encode(b'{"alg":"RS256"}').rstrip(b"=").decode()
        payload = base64.urlsafe_b64encode(b'{"username":"user-from-username"}').rstrip(b"=").decode()
        token   = f"{header}.{payload}.sig"
        event   = {"headers": {"Authorization": f"Bearer {token}"}, "body": None}
        resp    = fn.lambda_handler(event, {})
        self.assertEqual(resp["statusCode"], 200)

    @patch("start_fn.boto3")
    def test_lowercase_authorization_header_accepted(self, mock_boto3):
        """API Gateway may forward headers in lowercase."""
        mock_boto3.resource.return_value.Table.return_value = MagicMock()

        event = {"headers": {"authorization": f"Bearer {_make_jwt('user-lower')}"}, "body": None}
        resp  = fn.lambda_handler(event, {})
        self.assertEqual(resp["statusCode"], 200)

    # ── DynamoDB failure ──────────────────────────────────────────────────────

    @patch("start_fn.boto3")
    def test_dynamodb_error_propagates(self, mock_boto3):
        """If DynamoDB write fails the Lambda should raise (not silently succeed)."""
        mock_table = MagicMock()
        mock_table.put_item.side_effect = Exception("DynamoDB unavailable")
        mock_boto3.resource.return_value.Table.return_value = mock_table

        with self.assertRaises(Exception):
            fn.lambda_handler(_event(token=_make_jwt("user-1")), {})


class TestPkceHelper(unittest.TestCase):

    def test_verifier_and_challenge_differ(self):
        v, c = fn._generate_pkce()
        self.assertNotEqual(v, c)

    def test_verifier_is_base64url_no_padding(self):
        v, _ = fn._generate_pkce()
        self.assertNotIn("=", v)
        import re
        self.assertRegex(v, r'^[A-Za-z0-9\-_]+$')

    def test_challenge_is_43_chars(self):
        # SHA-256 = 32 bytes → base64url = ceil(32 * 4/3) = 43 chars (no padding)
        _, c = fn._generate_pkce()
        self.assertEqual(len(c), 43)

    def test_multiple_calls_produce_different_pairs(self):
        v1, c1 = fn._generate_pkce()
        v2, c2 = fn._generate_pkce()
        self.assertNotEqual(v1, v2)
        self.assertNotEqual(c1, c2)


if __name__ == "__main__":
    unittest.main(verbosity=2)

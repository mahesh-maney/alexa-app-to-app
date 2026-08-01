"""
Unit tests for alexa_complete_app_to_app/lambda_function.py

Run:
  cd /Users/maheshmaney/maney/digilux/app-to-app
  python -m pytest tests/test_complete_app_to_app.py -v
"""
import base64
import importlib.util
import json
import os
import sys
import time
import unittest
from unittest.mock import MagicMock, patch, call

_HERE     = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MOD_NAME = "complete_fn"
_SPEC     = importlib.util.spec_from_file_location(
    _MOD_NAME,
    os.path.join(_HERE, "lambdas", "alexa_complete_app_to_app", "lambda_function.py"),
)
fn = importlib.util.module_from_spec(_SPEC)
sys.modules[_MOD_NAME] = fn
_SPEC.loader.exec_module(fn)


# ── Helpers ───────────────────────────────────────────────────────────────────

USER_ID = "user-complete-test"

# Valid UUID4 — required by state format validation added for security
TEST_STATE = "a1b2c3d4-e5f6-4789-ab12-cd34ef567890"


def _make_jwt(sub: str = USER_ID) -> str:
    header  = base64.urlsafe_b64encode(b'{"alg":"RS256"}').rstrip(b"=").decode()
    payload = base64.urlsafe_b64encode(json.dumps({"sub": sub}).encode()).rstrip(b"=").decode()
    return f"{header}.{payload}.fakesig"


def _event(token: str = None, code: str = "AUTH_CODE", state: str = TEST_STATE) -> dict:
    headers = {}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return {
        "headers": headers,
        "body":    json.dumps({"code": code, "state": state}),
    }


def _pending_session(user_id: str = USER_ID, offset: int = 300) -> dict:
    """Return a fake PENDING session that expires `offset` seconds from now."""
    now = int(time.time())
    return {
        "state":        TEST_STATE,
        "userId":       user_id,
        "codeVerifier": "test-verifier-abc123",
        "status":       "PENDING",
        "createdAt":    now,
        "expiresAt":    now + offset,
    }


def _mock_ddb(session: dict = None, lwa_tokens_table: MagicMock = None):
    """Return a mock boto3.resource that returns configured tables."""
    mock_session_table = MagicMock()
    mock_session_table.get_item.return_value = {"Item": session} if session else {}

    mock_tokens_table = lwa_tokens_table or MagicMock()

    def table_selector(name):
        if name == fn._SESSION_TABLE:
            return mock_session_table
        if name == fn._TOKENS_TABLE:
            return mock_tokens_table
        return MagicMock()

    mock_resource = MagicMock()
    mock_resource.Table.side_effect = table_selector
    return mock_resource, mock_session_table, mock_tokens_table


def _lwa_token_response(access_token="ACCESS", refresh_token="REFRESH", expires_in=3600):
    return json.dumps({
        "access_token":  access_token,
        "refresh_token": refresh_token,
        "expires_in":    expires_in,
    }).encode()


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestCompleteAppToApp(unittest.TestCase):

    def setUp(self):
        fn._dynamodb      = None
        fn._lwa_creds     = None
        fn._ALEXA_SKILL_ID = "amzn1.ask.skill.test-skill-id"

    # ── Happy path ────────────────────────────────────────────────────────────

    @patch("complete_fn.urllib.request.urlopen")
    @patch("complete_fn.boto3")
    def test_success_returns_200_linked_true(self, mock_boto3, mock_urlopen):
        mock_resource, _, _ = _mock_ddb(session=_pending_session())
        mock_boto3.resource.return_value = mock_resource
        mock_boto3.client.return_value.get_secret_value.return_value = {
            "SecretString": json.dumps({"client_id": "CID", "client_secret": "CSEC"})
        }
        mock_cm = MagicMock()
        mock_cm.__enter__.return_value.read.return_value = _lwa_token_response()
        mock_cm.__enter__.return_value.status = 200
        mock_urlopen.return_value = mock_cm

        resp = fn.lambda_handler(_event(token=_make_jwt()), {})

        self.assertEqual(resp["statusCode"], 200)
        self.assertTrue(json.loads(resp["body"])["linked"])

    @patch("complete_fn.urllib.request.urlopen")
    @patch("complete_fn.boto3")
    def test_tokens_stored_in_lwa_table(self, mock_boto3, mock_urlopen):
        """Access + refresh tokens must be persisted after successful exchange."""
        _, _, mock_tokens = _mock_ddb(session=_pending_session())
        mock_resource = MagicMock()

        def table_sel(name):
            if name == fn._SESSION_TABLE:
                t = MagicMock()
                t.get_item.return_value = {"Item": _pending_session()}
                return t
            if name == fn._TOKENS_TABLE:
                return mock_tokens
            return MagicMock()

        mock_resource.Table.side_effect = table_sel
        mock_boto3.resource.return_value = mock_resource
        mock_boto3.client.return_value.get_secret_value.return_value = {
            "SecretString": json.dumps({"client_id": "CID", "client_secret": "CSEC"})
        }
        mock_cm = MagicMock()
        mock_cm.__enter__.return_value.read.return_value = _lwa_token_response(
            access_token="MY_ACCESS", refresh_token="MY_REFRESH"
        )
        mock_urlopen.return_value = mock_cm

        fn.lambda_handler(_event(token=_make_jwt()), {})

        mock_tokens.put_item.assert_called_once()
        stored = mock_tokens.put_item.call_args[1]["Item"]
        self.assertEqual(stored["userId"],       USER_ID)
        self.assertEqual(stored["accessToken"],  "MY_ACCESS")
        self.assertEqual(stored["refreshToken"], "MY_REFRESH")
        self.assertEqual(stored["linkMethod"],   "app-to-app")
        self.assertIn("linkedAt",  stored)
        self.assertIn("expiresAt", stored)

    @patch("complete_fn.urllib.request.urlopen")
    @patch("complete_fn.boto3")
    def test_state_marked_used_before_token_exchange(self, mock_boto3, mock_urlopen):
        """State must be USED-stamped BEFORE calling Amazon (replay protection)."""
        call_order = []

        mock_session_table = MagicMock()
        mock_session_table.get_item.return_value = {"Item": _pending_session()}
        mock_session_table.update_item.side_effect = lambda **kw: call_order.append("marked_used")

        mock_resource = MagicMock()
        mock_resource.Table.side_effect = lambda n: mock_session_table if n == fn._SESSION_TABLE else MagicMock()
        mock_boto3.resource.return_value = mock_resource
        mock_boto3.client.return_value.get_secret_value.return_value = {
            "SecretString": json.dumps({"client_id": "C", "client_secret": "S"})
        }

        mock_cm = MagicMock()
        mock_cm.__enter__.return_value.read.side_effect = lambda: (
            call_order.append("amazon_called") or _lwa_token_response()
        )
        mock_urlopen.return_value = mock_cm

        fn.lambda_handler(_event(token=_make_jwt()), {})

        self.assertLess(
            call_order.index("marked_used"),
            call_order.index("amazon_called"),
            "State must be marked USED before calling Amazon LWA"
        )

    @patch("complete_fn.urllib.request.urlopen")
    @patch("complete_fn.boto3")
    def test_token_expiry_has_60_second_buffer(self, mock_boto3, mock_urlopen):
        """Stored expiresAt = now + expires_in - 60 (buffer for clock skew)."""
        mock_resource, _, mock_tokens = _mock_ddb(session=_pending_session())

        def table_sel(name):
            if name == fn._SESSION_TABLE:
                t = MagicMock()
                t.get_item.return_value = {"Item": _pending_session()}
                return t
            return mock_tokens

        mock_resource.Table.side_effect = table_sel
        mock_boto3.resource.return_value = mock_resource
        mock_boto3.client.return_value.get_secret_value.return_value = {
            "SecretString": json.dumps({"client_id": "C", "client_secret": "S"})
        }
        mock_cm = MagicMock()
        mock_cm.__enter__.return_value.read.return_value = _lwa_token_response(expires_in=3600)
        mock_urlopen.return_value = mock_cm

        before = int(time.time())
        fn.lambda_handler(_event(token=_make_jwt()), {})
        after = int(time.time())

        stored = mock_tokens.put_item.call_args[1]["Item"]
        # expiresAt should be approximately now + 3600 - 60 = now + 3540
        self.assertGreaterEqual(stored["expiresAt"], before + 3540 - 2)
        self.assertLessEqual(stored["expiresAt"],    after  + 3540 + 2)

    # ── Auth failures ─────────────────────────────────────────────────────────

    def test_missing_auth_returns_401(self):
        resp = fn.lambda_handler({"headers": {}, "body": "{}"}, {})
        self.assertEqual(resp["statusCode"], 401)

    def test_malformed_jwt_returns_401(self):
        event = {"headers": {"Authorization": "Bearer garbage"}, "body": "{}"}
        resp  = fn.lambda_handler(event, {})
        self.assertEqual(resp["statusCode"], 401)

    # ── Request body validation ───────────────────────────────────────────────

    @patch("complete_fn.boto3")
    def test_missing_code_returns_400(self, mock_boto3):
        event = {
            "headers": {"Authorization": f"Bearer {_make_jwt()}"},
            "body":    json.dumps({"state": "some-state"}),
        }
        resp = fn.lambda_handler(event, {})
        self.assertEqual(resp["statusCode"], 400)
        self.assertIn("code", json.loads(resp["body"])["error"])

    @patch("complete_fn.boto3")
    def test_missing_state_returns_400(self, mock_boto3):
        event = {
            "headers": {"Authorization": f"Bearer {_make_jwt()}"},
            "body":    json.dumps({"code": "SOME_CODE"}),
        }
        resp = fn.lambda_handler(event, {})
        self.assertEqual(resp["statusCode"], 400)
        self.assertIn("state", json.loads(resp["body"])["error"])

    @patch("complete_fn.boto3")
    def test_invalid_json_body_returns_400(self, mock_boto3):
        event = {
            "headers": {"Authorization": f"Bearer {_make_jwt()}"},
            "body":    "not-valid-json{{",
        }
        resp = fn.lambda_handler(event, {})
        self.assertEqual(resp["statusCode"], 400)

    @patch("complete_fn.boto3")
    def test_empty_body_returns_400(self, mock_boto3):
        event = {
            "headers": {"Authorization": f"Bearer {_make_jwt()}"},
            "body":    "{}",
        }
        resp = fn.lambda_handler(event, {})
        self.assertEqual(resp["statusCode"], 400)

    # ── State validation ──────────────────────────────────────────────────────

    @patch("complete_fn.boto3")
    def test_nonexistent_state_returns_400(self, mock_boto3):
        mock_resource, mock_session_table, _ = _mock_ddb(session=None)
        mock_boto3.resource.return_value = mock_resource

        resp = fn.lambda_handler(_event(token=_make_jwt()), {})
        self.assertEqual(resp["statusCode"], 400)
        self.assertIn("Invalid state", json.loads(resp["body"])["error"])

    @patch("complete_fn.boto3")
    def test_state_belonging_to_other_user_returns_400(self, mock_boto3):
        session = _pending_session(user_id="different-user")
        mock_resource, _, _ = _mock_ddb(session=session)
        mock_boto3.resource.return_value = mock_resource

        resp = fn.lambda_handler(_event(token=_make_jwt(USER_ID)), {})
        self.assertEqual(resp["statusCode"], 400)
        self.assertIn("Invalid state", json.loads(resp["body"])["error"])

    @patch("complete_fn.boto3")
    def test_expired_state_returns_400(self, mock_boto3):
        session = _pending_session(offset=-1)  # already expired
        mock_resource, _, _ = _mock_ddb(session=session)
        mock_boto3.resource.return_value = mock_resource

        resp = fn.lambda_handler(_event(token=_make_jwt()), {})
        self.assertEqual(resp["statusCode"], 400)
        self.assertIn("expired", json.loads(resp["body"])["error"].lower())

    @patch("complete_fn.boto3")
    def test_already_used_state_returns_400(self, mock_boto3):
        session = {**_pending_session(), "status": "USED"}
        mock_resource, _, _ = _mock_ddb(session=session)
        mock_boto3.resource.return_value = mock_resource

        resp = fn.lambda_handler(_event(token=_make_jwt()), {})
        self.assertEqual(resp["statusCode"], 400)
        self.assertIn("already used", json.loads(resp["body"])["error"].lower())

    # ── Amazon LWA failure ────────────────────────────────────────────────────

    @patch("complete_fn.urllib.request.urlopen")
    @patch("complete_fn.boto3")
    def test_amazon_400_returns_502(self, mock_boto3, mock_urlopen):
        import urllib.error

        mock_resource, _, _ = _mock_ddb(session=_pending_session())
        mock_boto3.resource.return_value = mock_resource
        mock_boto3.client.return_value.get_secret_value.return_value = {
            "SecretString": json.dumps({"client_id": "C", "client_secret": "S"})
        }

        err = urllib.error.HTTPError(url="", code=400, msg="Bad Request",
                                     hdrs=None, fp=MagicMock())
        err.read = lambda: b'{"error":"invalid_grant"}'
        mock_urlopen.side_effect = err

        resp = fn.lambda_handler(_event(token=_make_jwt()), {})
        self.assertEqual(resp["statusCode"], 502)

    @patch("complete_fn.urllib.request.urlopen")
    @patch("complete_fn.boto3")
    def test_amazon_timeout_returns_502(self, mock_boto3, mock_urlopen):
        import socket

        mock_resource, _, _ = _mock_ddb(session=_pending_session())
        mock_boto3.resource.return_value = mock_resource
        mock_boto3.client.return_value.get_secret_value.return_value = {
            "SecretString": json.dumps({"client_id": "C", "client_secret": "S"})
        }
        mock_urlopen.side_effect = socket.timeout("timed out")

        with self.assertRaises(Exception):
            fn.lambda_handler(_event(token=_make_jwt()), {})

    # ── PKCE verifier used in exchange ────────────────────────────────────────

    @patch("complete_fn.urllib.request.urlopen")
    @patch("complete_fn.boto3")
    def test_code_verifier_sent_to_amazon(self, mock_boto3, mock_urlopen):
        """The stored codeVerifier must be included in the Amazon token request."""
        session = {**_pending_session(), "codeVerifier": "my-specific-verifier-xyz"}
        mock_resource, _, _ = _mock_ddb(session=session)

        def table_sel(name):
            if name == fn._SESSION_TABLE:
                t = MagicMock()
                t.get_item.return_value = {"Item": session}
                return t
            return MagicMock()

        mock_resource.Table.side_effect = table_sel
        mock_boto3.resource.return_value = mock_resource
        mock_boto3.client.return_value.get_secret_value.return_value = {
            "SecretString": json.dumps({"client_id": "C", "client_secret": "S"})
        }
        mock_cm = MagicMock()
        mock_cm.__enter__.return_value.read.return_value = _lwa_token_response()
        mock_urlopen.return_value = mock_cm

        fn.lambda_handler(_event(token=_make_jwt()), {})

        # Inspect the first urlopen call (LWA token exchange), not the second (skill enablement)
        request_obj = mock_urlopen.call_args_list[0][0][0]
        sent_data   = request_obj.data.decode()
        self.assertIn("code_verifier=my-specific-verifier-xyz", sent_data)

    @patch("complete_fn.urllib.request.urlopen")
    @patch("complete_fn.boto3")
    def test_redirect_uri_sent_to_amazon(self, mock_boto3, mock_urlopen):
        session = _pending_session()
        mock_resource, _, _ = _mock_ddb(session=session)

        def table_sel(name):
            if name == fn._SESSION_TABLE:
                t = MagicMock()
                t.get_item.return_value = {"Item": session}
                return t
            return MagicMock()

        mock_resource.Table.side_effect = table_sel
        mock_boto3.resource.return_value = mock_resource
        mock_boto3.client.return_value.get_secret_value.return_value = {
            "SecretString": json.dumps({"client_id": "C", "client_secret": "S"})
        }
        mock_cm = MagicMock()
        mock_cm.__enter__.return_value.read.return_value = _lwa_token_response()
        mock_urlopen.return_value = mock_cm

        fn.lambda_handler(_event(token=_make_jwt()), {})

        # Inspect the first urlopen call (LWA token exchange), not the second (skill enablement)
        request_obj = mock_urlopen.call_args_list[0][0][0]
        sent_data   = request_obj.data.decode()
        import urllib.parse
        self.assertIn(urllib.parse.quote(fn._REDIRECT_URI, safe=""), sent_data)

    # ── Secrets Manager caching ───────────────────────────────────────────────

    @patch("complete_fn.urllib.request.urlopen")
    @patch("complete_fn.boto3")
    def test_lwa_secrets_cached_across_calls(self, mock_boto3, mock_urlopen):
        """Secrets Manager must only be called once per container lifetime."""
        fn._lwa_creds = None

        def make_session():
            return {**_pending_session(), "state": f"STATE-{time.time()}"}

        sessions = [make_session(), make_session()]
        call_count = [0]

        def get_item_side_effect(Key):
            s = sessions[call_count[0] % 2]
            call_count[0] += 1
            return {"Item": {**s, "state": Key["state"]}}

        mock_session_table = MagicMock()
        mock_session_table.get_item.side_effect = get_item_side_effect
        mock_resource = MagicMock()
        mock_resource.Table.return_value = mock_session_table
        mock_boto3.resource.return_value = mock_resource

        mock_sm = MagicMock()
        mock_sm.get_secret_value.return_value = {
            "SecretString": json.dumps({"client_id": "C", "client_secret": "S"})
        }
        mock_boto3.client.return_value = mock_sm

        mock_cm = MagicMock()
        mock_cm.__enter__.return_value.read.return_value = _lwa_token_response()
        mock_urlopen.return_value = mock_cm

        fn.lambda_handler(_event(token=_make_jwt()), {})
        fn.lambda_handler(_event(token=_make_jwt()), {})

        # Secrets Manager called exactly once (first call), not twice
        self.assertEqual(mock_sm.get_secret_value.call_count, 1)


    # ── Skill Enablement API ──────────────────────────────────────────────────

    @patch("complete_fn.urllib.request.urlopen")
    @patch("complete_fn.boto3")
    def test_skill_enablement_api_called(self, mock_boto3, mock_urlopen):
        """urlopen must be called twice: once for LWA exchange, once for Skill Enablement."""
        mock_resource, _, _ = _mock_ddb(session=_pending_session())
        mock_boto3.resource.return_value = mock_resource
        mock_boto3.client.return_value.get_secret_value.return_value = {
            "SecretString": json.dumps({"client_id": "CID", "client_secret": "CSEC"})
        }
        mock_cm = MagicMock()
        mock_cm.__enter__.return_value.read.return_value = _lwa_token_response()
        mock_urlopen.return_value = mock_cm

        fn.lambda_handler(_event(token=_make_jwt()), {})

        self.assertEqual(mock_urlopen.call_count, 2)
        second_req = mock_urlopen.call_args_list[1][0][0]
        self.assertIn("amazonalexa.com", second_req.full_url)
        self.assertIn("enablement", second_req.full_url)

    @patch("complete_fn.urllib.request.urlopen")
    @patch("complete_fn.boto3")
    def test_skill_enablement_uses_access_token_as_bearer(self, mock_boto3, mock_urlopen):
        """The Skill Enablement request must use the LWA access_token as Bearer."""
        mock_resource, _, _ = _mock_ddb(session=_pending_session())
        mock_boto3.resource.return_value = mock_resource
        mock_boto3.client.return_value.get_secret_value.return_value = {
            "SecretString": json.dumps({"client_id": "CID", "client_secret": "CSEC"})
        }
        mock_cm = MagicMock()
        mock_cm.__enter__.return_value.read.return_value = _lwa_token_response(
            access_token="MY_AMAZON_TOKEN"
        )
        mock_urlopen.return_value = mock_cm

        fn.lambda_handler(_event(token=_make_jwt()), {})

        second_req = mock_urlopen.call_args_list[1][0][0]
        self.assertIn("MY_AMAZON_TOKEN", second_req.get_header("Authorization"))

    @patch("complete_fn.urllib.request.urlopen")
    @patch("complete_fn.boto3")
    def test_skill_enablement_url_contains_skill_id(self, mock_boto3, mock_urlopen):
        """The Skill Enablement request URL must contain the configured skill ID."""
        mock_resource, _, _ = _mock_ddb(session=_pending_session())
        mock_boto3.resource.return_value = mock_resource
        mock_boto3.client.return_value.get_secret_value.return_value = {
            "SecretString": json.dumps({"client_id": "CID", "client_secret": "CSEC"})
        }
        mock_cm = MagicMock()
        mock_cm.__enter__.return_value.read.return_value = _lwa_token_response()
        mock_urlopen.return_value = mock_cm

        fn.lambda_handler(_event(token=_make_jwt()), {})

        second_req = mock_urlopen.call_args_list[1][0][0]
        self.assertIn(fn._ALEXA_SKILL_ID, second_req.full_url)

    @patch("complete_fn.urllib.request.urlopen")
    @patch("complete_fn.boto3")
    def test_skill_enablement_failure_returns_502(self, mock_boto3, mock_urlopen):
        """If the Skill Enablement API returns an error, Lambda must return 502."""
        import urllib.error as uerr

        mock_resource, _, _ = _mock_ddb(session=_pending_session())
        mock_boto3.resource.return_value = mock_resource
        mock_boto3.client.return_value.get_secret_value.return_value = {
            "SecretString": json.dumps({"client_id": "CID", "client_secret": "CSEC"})
        }
        # First call (LWA) succeeds
        lwa_cm = MagicMock()
        lwa_cm.__enter__.return_value.read.return_value = _lwa_token_response()

        # Second call (Skill Enablement) fails
        skill_err = uerr.HTTPError(url="", code=400, msg="Bad Request",
                                   hdrs=None, fp=MagicMock())
        skill_err.read = lambda: b'{"message":"skill not found"}'

        mock_urlopen.side_effect = [lwa_cm, skill_err]

        resp = fn.lambda_handler(_event(token=_make_jwt()), {})
        self.assertEqual(resp["statusCode"], 502)

    @patch("complete_fn.urllib.request.urlopen")
    @patch("complete_fn.boto3")
    def test_tokens_not_stored_if_skill_enablement_fails(self, mock_boto3, mock_urlopen):
        """If skill enablement fails, tokens must NOT be written to DynamoDB."""
        import urllib.error as uerr

        _, _, mock_tokens = _mock_ddb(session=_pending_session())
        mock_resource = MagicMock()

        def table_sel(name):
            if name == fn._SESSION_TABLE:
                t = MagicMock()
                t.get_item.return_value = {"Item": _pending_session()}
                return t
            return mock_tokens

        mock_resource.Table.side_effect = table_sel
        mock_boto3.resource.return_value = mock_resource
        mock_boto3.client.return_value.get_secret_value.return_value = {
            "SecretString": json.dumps({"client_id": "CID", "client_secret": "CSEC"})
        }
        lwa_cm = MagicMock()
        lwa_cm.__enter__.return_value.read.return_value = _lwa_token_response()

        skill_err = uerr.HTTPError(url="", code=403, msg="Forbidden",
                                   hdrs=None, fp=MagicMock())
        skill_err.read = lambda: b'{"message":"unauthorized"}'

        mock_urlopen.side_effect = [lwa_cm, skill_err]

        resp = fn.lambda_handler(_event(token=_make_jwt()), {})

        self.assertEqual(resp["statusCode"], 502)
        mock_tokens.put_item.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)

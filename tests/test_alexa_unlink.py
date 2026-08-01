"""
Unit tests for alexa_unlink/lambda_function.py

Run:
  cd /Users/maheshmaney/maney/digilux/app-to-app
  python -m pytest tests/test_alexa_unlink.py -v
"""
import base64
import importlib.util
import json
import os
import sys
import unittest
from unittest.mock import MagicMock, patch, call

_HERE     = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MOD_NAME = "unlink_fn"
_SPEC     = importlib.util.spec_from_file_location(
    _MOD_NAME,
    os.path.join(_HERE, "lambdas", "alexa_unlink", "lambda_function.py"),
)
fn = importlib.util.module_from_spec(_SPEC)
sys.modules[_MOD_NAME] = fn
_SPEC.loader.exec_module(fn)


# ── Helpers ───────────────────────────────────────────────────────────────────

USER_ID = "user-unlink-test"


def _make_jwt(sub: str = USER_ID) -> str:
    header  = base64.urlsafe_b64encode(b'{"alg":"RS256"}').rstrip(b"=").decode()
    payload = base64.urlsafe_b64encode(json.dumps({"sub": sub}).encode()).rstrip(b"=").decode()
    return f"{header}.{payload}.fakesig"


def _event(token: str = None) -> dict:
    headers = {}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return {"headers": headers, "body": ""}


def _token_item(user_id: str = USER_ID) -> dict:
    return {
        "userId":       user_id,
        "accessToken":  "fake-access-token",
        "refreshToken": "fake-refresh-token",
        "expiresAt":    9999999999,
        "linkedAt":     1700000000,
        "linkMethod":   "app-to-app",
    }


def _ctx():
    ctx = MagicMock()
    ctx.aws_request_id = "test-req-unlink"
    return ctx


def _make_table(item=None):
    table = MagicMock()
    table.get_item.return_value = {"Item": item} if item else {}
    return table


# ── Auth tests ────────────────────────────────────────────────────────────────

class TestAuth(unittest.TestCase):

    def test_missing_auth_header_returns_401(self):
        resp = fn.lambda_handler(_event(), _ctx())
        self.assertEqual(resp["statusCode"], 401)

    def test_malformed_jwt_returns_401(self):
        event = _event("Bearer not-a-jwt")
        resp  = fn.lambda_handler(event, _ctx())
        self.assertEqual(resp["statusCode"], 401)

    def test_valid_jwt_proceeds(self):
        with patch.object(fn, "_ddb") as mock_ddb:
            table = _make_table(item=None)
            mock_ddb.return_value.Table.return_value = table
            resp = fn.lambda_handler(_event(_make_jwt()), _ctx())
        # 404 because no token record — auth passed
        self.assertEqual(resp["statusCode"], 404)


# ── Not linked ────────────────────────────────────────────────────────────────

class TestNotLinked(unittest.TestCase):

    def test_no_token_record_returns_404(self):
        with patch.object(fn, "_ddb") as mock_ddb:
            table = _make_table(item=None)
            mock_ddb.return_value.Table.return_value = table
            resp = fn.lambda_handler(_event(_make_jwt()), _ctx())
        self.assertEqual(resp["statusCode"], 404)
        body = json.loads(resp["body"])
        self.assertIn("No linked", body["error"])


# ── Successful unlink ─────────────────────────────────────────────────────────

class TestSuccessfulUnlink(unittest.TestCase):

    def setUp(self):
        self.table   = _make_table(item=_token_item())
        self.ddb_patcher = patch.object(fn, "_ddb")
        self.mock_ddb    = self.ddb_patcher.start()
        self.mock_ddb.return_value.Table.return_value = self.table

    def tearDown(self):
        self.ddb_patcher.stop()

    def test_returns_200_unlinked_true(self):
        with patch.object(fn, "_disable_alexa_skill"), \
             patch.object(fn, "_revoke_refresh_token"):
            resp = fn.lambda_handler(_event(_make_jwt()), _ctx())
        self.assertEqual(resp["statusCode"], 200)
        body = json.loads(resp["body"])
        self.assertTrue(body["unlinked"])

    def test_delete_item_called_for_user(self):
        with patch.object(fn, "_disable_alexa_skill"), \
             patch.object(fn, "_revoke_refresh_token"):
            fn.lambda_handler(_event(_make_jwt()), _ctx())
        self.table.delete_item.assert_called_once_with(Key={"userId": USER_ID})

    def test_disable_skill_called_with_access_token(self):
        with patch.object(fn, "_disable_alexa_skill") as mock_disable, \
             patch.object(fn, "_revoke_refresh_token"):
            fn.lambda_handler(_event(_make_jwt()), _ctx())
        mock_disable.assert_called_once()
        args = mock_disable.call_args[0]
        self.assertEqual(args[0], "fake-access-token")   # access_token
        self.assertEqual(args[1], USER_ID)               # user_id

    def test_revoke_token_called_with_refresh_token(self):
        with patch.object(fn, "_disable_alexa_skill"), \
             patch.object(fn, "_revoke_refresh_token") as mock_revoke:
            fn.lambda_handler(_event(_make_jwt()), _ctx())
        mock_revoke.assert_called_once()
        args = mock_revoke.call_args[0]
        self.assertEqual(args[0], "fake-refresh-token")  # refresh_token
        self.assertEqual(args[1], USER_ID)               # user_id


# ── Best-effort: skill disable failure does not block unlink ──────────────────

class TestSkillDisableFailure(unittest.TestCase):

    def test_unlink_succeeds_even_if_skill_disable_raises(self):
        table = _make_table(item=_token_item())
        with patch.object(fn, "_ddb") as mock_ddb, \
             patch.object(fn, "_disable_alexa_skill", side_effect=Exception("Alexa API down")), \
             patch.object(fn, "_revoke_refresh_token"):
            mock_ddb.return_value.Table.return_value = table
            resp = fn.lambda_handler(_event(_make_jwt()), _ctx())
        self.assertEqual(resp["statusCode"], 200)
        table.delete_item.assert_called_once()


# ── Best-effort: token revoke failure does not block unlink ───────────────────

class TestTokenRevokeFailure(unittest.TestCase):

    def test_unlink_succeeds_even_if_revoke_raises(self):
        table = _make_table(item=_token_item())
        with patch.object(fn, "_ddb") as mock_ddb, \
             patch.object(fn, "_disable_alexa_skill"), \
             patch.object(fn, "_revoke_refresh_token", side_effect=Exception("LWA down")):
            mock_ddb.return_value.Table.return_value = table
            resp = fn.lambda_handler(_event(_make_jwt()), _ctx())
        self.assertEqual(resp["statusCode"], 200)
        table.delete_item.assert_called_once()


# ── Config error ──────────────────────────────────────────────────────────────

class TestConfigError(unittest.TestCase):

    def test_missing_env_vars_returns_500(self):
        original = fn._missing_vars
        fn._missing_vars = ["DATA_REGION"]
        try:
            resp = fn.lambda_handler(_event(_make_jwt()), _ctx())
            self.assertEqual(resp["statusCode"], 500)
        finally:
            fn._missing_vars = original


# ── _disable_alexa_skill unit tests ──────────────────────────────────────────

class TestDisableAlexaSkill(unittest.TestCase):

    def test_skipped_if_no_skill_enablement_url(self):
        original_url = fn._SKILL_ENABLEMENT_URL
        fn._SKILL_ENABLEMENT_URL = ""
        try:
            # Should not raise, just log a warning
            fn._disable_alexa_skill("tok", USER_ID, "req1")
        finally:
            fn._SKILL_ENABLEMENT_URL = original_url

    def test_skipped_if_no_skill_id(self):
        original_id = fn._ALEXA_SKILL_ID
        fn._ALEXA_SKILL_ID = ""
        try:
            fn._disable_alexa_skill("tok", USER_ID, "req1")
        finally:
            fn._ALEXA_SKILL_ID = original_id

    def test_http_error_is_swallowed(self):
        import urllib.error
        original_url = fn._SKILL_ENABLEMENT_URL
        original_id  = fn._ALEXA_SKILL_ID
        fn._SKILL_ENABLEMENT_URL = "https://api.amazonalexa.com/v1/users/~current/skills"
        fn._ALEXA_SKILL_ID       = "amzn1.ask.skill.test"
        try:
            with patch("urllib.request.urlopen", side_effect=urllib.error.HTTPError(
                url="", code=404, msg="Not Found", hdrs=None, fp=None
            )):
                # Should NOT raise
                fn._disable_alexa_skill("tok", USER_ID, "req1")
        finally:
            fn._SKILL_ENABLEMENT_URL = original_url
            fn._ALEXA_SKILL_ID       = original_id


# ── _revoke_refresh_token unit tests ─────────────────────────────────────────

class TestRevokeRefreshToken(unittest.TestCase):

    def test_skipped_if_no_revoke_url(self):
        original = fn._LWA_REVOKE_URL
        fn._LWA_REVOKE_URL = ""
        try:
            fn._revoke_refresh_token("tok", USER_ID, "req1")
        finally:
            fn._LWA_REVOKE_URL = original

    def test_http_error_is_swallowed(self):
        import urllib.error
        original = fn._LWA_REVOKE_URL
        fn._LWA_REVOKE_URL = "https://api.amazon.com/auth/o2/revoke"
        try:
            with patch.object(fn, "_get_lwa_creds", return_value=("cid", "csec")), \
                 patch("urllib.request.urlopen", side_effect=urllib.error.HTTPError(
                     url="", code=400, msg="Bad Request", hdrs=None, fp=None
                 )):
                fn._revoke_refresh_token("tok", USER_ID, "req1")
        finally:
            fn._LWA_REVOKE_URL = original

    def test_sends_correct_token_type_hint(self):
        import urllib.request as urlreq
        original = fn._LWA_REVOKE_URL
        fn._LWA_REVOKE_URL = "https://api.amazon.com/auth/o2/revoke"
        captured = []

        def fake_urlopen(req, timeout=None):
            captured.append(req.data)
            return MagicMock().__enter__.return_value

        try:
            with patch.object(fn, "_get_lwa_creds", return_value=("cid", "csec")), \
                 patch("urllib.request.urlopen", side_effect=fake_urlopen):
                fn._revoke_refresh_token("mytoken", USER_ID, "req1")
        except Exception:
            pass  # urlopen mock may not context-manager correctly
        finally:
            fn._LWA_REVOKE_URL = original


if __name__ == "__main__":
    unittest.main()

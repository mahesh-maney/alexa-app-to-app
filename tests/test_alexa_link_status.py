"""Unit tests for alexa_link_status Lambda."""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from unittest.mock import MagicMock, patch

# ── Module loader ─────────────────────────────────────────────────────────────
_HERE     = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MOD_NAME = "status_fn"
_SPEC     = importlib.util.spec_from_file_location(
    _MOD_NAME,
    os.path.join(_HERE, "lambdas", "alexa_link_status", "lambda_function.py"),
)

def _load():
    sys.modules.pop(_MOD_NAME, None)
    m = importlib.util.module_from_spec(_SPEC)
    _SPEC.loader.exec_module(m)
    sys.modules[_MOD_NAME] = m
    return m

# ── Helpers ───────────────────────────────────────────────────────────────────
USER_ID = "user-123"
import base64 as _b64
VALID_TOKEN = (
    "header."
    + _b64.urlsafe_b64encode(json.dumps({"sub": USER_ID}).encode()).decode().rstrip("=")
    + ".sig"
)


def _event(token=None):
    auth = f"Bearer {token or VALID_TOKEN}"
    return {"headers": {"Authorization": auth}}


def _token_item(linked_at=1700000000):
    return {"userId": USER_ID, "linkedAt": linked_at, "accessToken": "enc_at", "refreshToken": "enc_rt"}


# ── Auth tests ────────────────────────────────────────────────────────────────
class TestAuth:
    def test_missing_header_returns_401(self):
        m = _load()
        r = m.lambda_handler({"headers": {}}, MagicMock())
        assert r["statusCode"] == 401

    def test_no_headers_key_returns_401(self):
        m = _load()
        r = m.lambda_handler({}, MagicMock())
        assert r["statusCode"] == 401

    def test_non_bearer_returns_401(self):
        m = _load()
        r = m.lambda_handler({"headers": {"Authorization": "Basic abc"}}, MagicMock())
        assert r["statusCode"] == 401

    def test_malformed_jwt_returns_401(self):
        m = _load()
        r = m.lambda_handler({"headers": {"Authorization": "Bearer notajwt"}}, MagicMock())
        assert r["statusCode"] == 401

    def test_lowercase_authorization_header_accepted(self):
        m = _load()
        with patch.object(m, "_ddb") as mock_ddb:
            mock_ddb.return_value.Table.return_value.get_item.return_value = {"Item": None}
            r = m.lambda_handler({"headers": {"authorization": f"Bearer {VALID_TOKEN}"}}, MagicMock())
        assert r["statusCode"] == 200


# ── Not linked ────────────────────────────────────────────────────────────────
class TestNotLinked:
    def test_returns_linked_false_when_no_record(self):
        m = _load()
        with patch.object(m, "_ddb") as mock_ddb:
            mock_ddb.return_value.Table.return_value.get_item.return_value = {}
            r = m.lambda_handler(_event(), MagicMock())
        assert r["statusCode"] == 200
        body = json.loads(r["body"])
        assert body == {"linked": False}

    def test_returns_linked_false_when_item_is_none(self):
        m = _load()
        with patch.object(m, "_ddb") as mock_ddb:
            mock_ddb.return_value.Table.return_value.get_item.return_value = {"Item": None}
            r = m.lambda_handler(_event(), MagicMock())
        assert r["statusCode"] == 200
        body = json.loads(r["body"])
        assert body["linked"] is False


# ── Linked ────────────────────────────────────────────────────────────────────
class TestLinked:
    def _run(self, item):
        m = _load()
        with patch.object(m, "_ddb") as mock_ddb:
            mock_ddb.return_value.Table.return_value.get_item.return_value = {"Item": item}
            r = m.lambda_handler(_event(), MagicMock())
        return r

    def test_returns_linked_true_when_record_exists(self):
        r = self._run(_token_item())
        assert r["statusCode"] == 200
        body = json.loads(r["body"])
        assert body["linked"] is True

    def test_returns_linked_at_timestamp(self):
        r = self._run(_token_item(linked_at=1700000000))
        body = json.loads(r["body"])
        assert body["linkedAt"] == 1700000000

    def test_linked_at_omitted_when_not_in_record(self):
        r = self._run({"userId": USER_ID, "accessToken": "enc"})
        body = json.loads(r["body"])
        assert body["linked"] is True
        assert "linkedAt" not in body

    def test_queries_correct_table(self):
        m = _load()
        with patch.object(m, "_ddb") as mock_ddb:
            mock_table = mock_ddb.return_value.Table.return_value
            mock_table.get_item.return_value = {"Item": _token_item()}
            m.lambda_handler(_event(), MagicMock())
        mock_ddb.return_value.Table.assert_called_with(m._TOKENS_TABLE)

    def test_queries_correct_user_id(self):
        m = _load()
        with patch.object(m, "_ddb") as mock_ddb:
            mock_table = mock_ddb.return_value.Table.return_value
            mock_table.get_item.return_value = {"Item": _token_item()}
            m.lambda_handler(_event(), MagicMock())
        mock_table.get_item.assert_called_once_with(Key={"userId": USER_ID})


# ── Config error ──────────────────────────────────────────────────────────────
class TestConfigError:
    def test_missing_env_vars_returns_500(self):
        import os
        original = os.environ.get("LWA_TOKENS_TABLE")
        os.environ["LWA_TOKENS_TABLE"] = ""
        m = _load()
        r = m.lambda_handler(_event(), MagicMock())
        assert r["statusCode"] == 500
        if original is not None:
            os.environ["LWA_TOKENS_TABLE"] = original
        else:
            del os.environ["LWA_TOKENS_TABLE"]


# ── DDB error ─────────────────────────────────────────────────────────────────
class TestDdbError:
    def test_ddb_exception_returns_500(self):
        m = _load()
        with patch.object(m, "_ddb") as mock_ddb:
            mock_ddb.return_value.Table.return_value.get_item.side_effect = Exception("DDB down")
            r = m.lambda_handler(_event(), MagicMock())
        assert r["statusCode"] == 500

    def test_ddb_exception_body_has_error_key(self):
        m = _load()
        with patch.object(m, "_ddb") as mock_ddb:
            mock_ddb.return_value.Table.return_value.get_item.side_effect = Exception("DDB down")
            r = m.lambda_handler(_event(), MagicMock())
        body = json.loads(r["body"])
        assert "error" in body


# ── Response shape ────────────────────────────────────────────────────────────
class TestResponseShape:
    def test_cors_header_present(self):
        m = _load()
        with patch.object(m, "_ddb") as mock_ddb:
            mock_ddb.return_value.Table.return_value.get_item.return_value = {}
            r = m.lambda_handler(_event(), MagicMock())
        assert r["headers"]["Access-Control-Allow-Origin"] == "*"

    def test_content_type_is_json(self):
        m = _load()
        with patch.object(m, "_ddb") as mock_ddb:
            mock_ddb.return_value.Table.return_value.get_item.return_value = {}
            r = m.lambda_handler(_event(), MagicMock())
        assert r["headers"]["Content-Type"] == "application/json"

    def test_body_is_valid_json_string(self):
        m = _load()
        with patch.object(m, "_ddb") as mock_ddb:
            mock_ddb.return_value.Table.return_value.get_item.return_value = {}
            r = m.lambda_handler(_event(), MagicMock())
        body = json.loads(r["body"])
        assert isinstance(body, dict)

"""Unit tests for google_home_unlink Lambda.

DELETE /api/v1/voice/google-home/account-linking
"""
from __future__ import annotations

import base64 as _b64
import importlib.util
import json
import os
import sys
from unittest.mock import MagicMock, patch

from botocore.exceptions import ClientError

# ── Module loader ──────────────────────────────────────────────────────────────
_HERE     = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MOD_NAME = "gh_unlink_fn"
_SPEC     = importlib.util.spec_from_file_location(
    _MOD_NAME,
    os.path.join(_HERE, "lambdas", "google_home_unlink", "lambda_function.py"),
)


def _load():
    sys.modules.pop(_MOD_NAME, None)
    m = importlib.util.module_from_spec(_SPEC)
    _SPEC.loader.exec_module(m)
    sys.modules[_MOD_NAME] = m
    return m


# ── Test helpers ──────────────────────────────────────────────────────────────
USER_ID = "user-sub-unlink-abc"
VALID_TOKEN = (
    "header."
    + _b64.urlsafe_b64encode(json.dumps({"sub": USER_ID}).encode()).decode().rstrip("=")
    + ".sig"
)


def _event(token=None):
    return {
        "httpMethod": "DELETE",
        "path": "/api/v1/voice/google-home/account-linking",
        "headers": {"Authorization": f"Bearer {token or VALID_TOKEN}"},
        "requestContext": {"identity": {"sourceIp": "1.2.3.4"}},
    }


def _token_record():
    return {
        "userId":       USER_ID,
        "agentId":      "test-agent-id",
        "linkedAt":     "1700000000",
        "accessToken":  "at_abc123",
        "refreshToken": "rt_abc123",
        "scope":        "profile",
    }


def _run_unlink(record):
    """Run unlink with a given token record (or None for already-unlinked).
    Always mocks _delete_agent_user to prevent outbound network calls."""
    m = _load()
    with patch.object(m, "_ddb") as mock_ddb, \
         patch.object(m, "_delete_agent_user") as mock_delete_agent:
        mock_ddb.return_value.Table.return_value.get_item.return_value = (
            {"Item": record} if record is not None else {}
        )
        mock_ddb.return_value.Table.return_value.delete_item.return_value = {}
        r = m.lambda_handler(_event(), MagicMock())
    return r, m, mock_ddb, mock_delete_agent


# ── Auth ──────────────────────────────────────────────────────────────────────
class TestAuth:
    def test_missing_header_returns_401(self):
        m = _load()
        r = m.lambda_handler({"headers": {}}, MagicMock())
        assert r["statusCode"] == 401

    def test_no_headers_key_returns_401(self):
        m = _load()
        r = m.lambda_handler({}, MagicMock())
        assert r["statusCode"] == 401

    def test_malformed_jwt_returns_401(self):
        m = _load()
        r = m.lambda_handler({"headers": {"Authorization": "Bearer notajwt"}}, MagicMock())
        assert r["statusCode"] == 401

    def test_lowercase_authorization_header_accepted(self):
        m = _load()
        with patch.object(m, "_ddb") as mock_ddb, \
             patch.object(m, "_delete_agent_user"):
            mock_ddb.return_value.Table.return_value.get_item.return_value = {}
            r = m.lambda_handler({"headers": {"authorization": f"Bearer {VALID_TOKEN}"}}, MagicMock())
        assert r["statusCode"] == 200


# ── Already unlinked (idempotent) ─────────────────────────────────────────────
class TestAlreadyUnlinked:
    def test_returns_200_when_no_record(self):
        r, _, _, _ = _run_unlink(None)
        assert r["statusCode"] == 200

    def test_returns_linked_false_when_no_record(self):
        r, _, _, _ = _run_unlink(None)
        assert json.loads(r["body"]) == {"linked": False}

    def test_delete_not_called_when_no_record(self):
        r, _, mock_ddb, _ = _run_unlink(None)
        mock_ddb.return_value.Table.return_value.delete_item.assert_not_called()

    def test_delete_agent_user_not_called_when_no_record(self):
        r, _, _, mock_delete_agent = _run_unlink(None)
        mock_delete_agent.assert_not_called()


# ── Successful unlink ─────────────────────────────────────────────────────────
class TestSuccessfulUnlink:
    def test_returns_200(self):
        r, _, _, _ = _run_unlink(_token_record())
        assert r["statusCode"] == 200

    def test_returns_linked_false(self):
        r, _, _, _ = _run_unlink(_token_record())
        assert json.loads(r["body"]) == {"linked": False}

    def test_delete_item_called_with_correct_user_id(self):
        r, m, mock_ddb, _ = _run_unlink(_token_record())
        mock_ddb.return_value.Table.return_value.delete_item.assert_called_once_with(
            Key={"userId": USER_ID}
        )

    def test_delete_item_called_on_tokens_table(self):
        r, m, mock_ddb, _ = _run_unlink(_token_record())
        mock_ddb.return_value.Table.assert_called_with(m._TOKENS_TABLE)

    def test_delete_agent_user_called_with_user_id(self):
        r, _, _, mock_delete_agent = _run_unlink(_token_record())
        mock_delete_agent.assert_called_once_with(USER_ID)

    def test_unlink_succeeds_even_if_delete_agent_user_raises(self):
        """Even if the Home Graph API call raises, DDB delete still runs."""
        m = _load()
        with patch.object(m, "_ddb") as mock_ddb, \
             patch.object(m, "_delete_agent_user", side_effect=RuntimeError("network down")):
            mock_ddb.return_value.Table.return_value.get_item.return_value = {"Item": _token_record()}
            mock_ddb.return_value.Table.return_value.delete_item.return_value = {}
            r = m.lambda_handler(_event(), MagicMock())
        mock_ddb.return_value.Table.return_value.delete_item.assert_called_once()
        assert r["statusCode"] == 200


# ── _delete_agent_user (unit tests for the Home Graph helper) ─────────────────
class TestDeleteAgentUser:
    def test_skips_when_no_sa_key_configured(self):
        """When GOOGLE_SA_KEY_SECRET_ARN is empty, skip without error."""
        m = _load()
        with patch.object(m, "_GH_SA_KEY_SECRET_ARN", ""):
            # Should not raise
            m._delete_agent_user(USER_ID)

    def test_skips_when_sa_creds_empty(self):
        """When _get_sa_credentials returns {}, skip without error."""
        m = _load()
        with patch.object(m, "_get_sa_credentials", return_value={}):
            m._delete_agent_user(USER_ID)

    def test_skips_when_sa_token_fetch_fails(self):
        """When the service account token exchange fails, skip without error."""
        m = _load()
        sa_creds = {"client_email": "sa@test.iam.gserviceaccount.com", "private_key": "---"}
        with patch.object(m, "_get_sa_credentials", return_value=sa_creds), \
             patch.object(m, "_get_sa_access_token", side_effect=Exception("token error")):
            m._delete_agent_user(USER_ID)

    def test_home_graph_http_error_is_swallowed(self):
        """Home Graph returning 4xx is logged but does not raise."""
        import urllib.error
        m = _load()
        sa_creds = {"client_email": "sa@test.iam.gserviceaccount.com", "private_key": "---"}
        with patch.object(m, "_get_sa_credentials", return_value=sa_creds), \
             patch.object(m, "_get_sa_access_token", return_value="fake_sa_token"), \
             patch("urllib.request.urlopen") as mock_open:
            mock_open.side_effect = urllib.error.HTTPError(
                url="https://homegraph.googleapis.com/v1/agentUsers/x",
                code=403, msg="Forbidden", hdrs=None, fp=None,
            )
            m._delete_agent_user(USER_ID)  # must not raise

    def test_home_graph_url_error_is_swallowed(self):
        m = _load()
        sa_creds = {"client_email": "sa@test.iam.gserviceaccount.com", "private_key": "---"}
        with patch.object(m, "_get_sa_credentials", return_value=sa_creds), \
             patch.object(m, "_get_sa_access_token", return_value="fake_sa_token"), \
             patch("urllib.request.urlopen") as mock_open:
            import urllib.error
            mock_open.side_effect = urllib.error.URLError("connection refused")
            m._delete_agent_user(USER_ID)

    def test_home_graph_success(self):
        m = _load()
        sa_creds = {"client_email": "sa@test.iam.gserviceaccount.com", "private_key": "---"}
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        with patch.object(m, "_get_sa_credentials", return_value=sa_creds), \
             patch.object(m, "_get_sa_access_token", return_value="fake_sa_token"), \
             patch("urllib.request.urlopen", return_value=mock_resp):
            m._delete_agent_user(USER_ID)  # must not raise


# ── DDB errors ────────────────────────────────────────────────────────────────
class TestDdbGetError:
    def test_client_error_on_get_returns_500(self):
        m = _load()
        with patch.object(m, "_ddb") as mock_ddb:
            mock_ddb.return_value.Table.return_value.get_item.side_effect = ClientError(
                {"Error": {"Code": "InternalServerError", "Message": "x"}}, "GetItem"
            )
            r = m.lambda_handler(_event(), MagicMock())
        assert r["statusCode"] == 500

    def test_unexpected_error_on_get_returns_500(self):
        m = _load()
        with patch.object(m, "_ddb") as mock_ddb:
            mock_ddb.return_value.Table.return_value.get_item.side_effect = RuntimeError("ddb down")
            r = m.lambda_handler(_event(), MagicMock())
        assert r["statusCode"] == 500


class TestDdbDeleteError:
    def test_client_error_on_delete_returns_500(self):
        m = _load()
        with patch.object(m, "_ddb") as mock_ddb, \
             patch.object(m, "_delete_agent_user"):
            mock_ddb.return_value.Table.return_value.get_item.return_value = {"Item": _token_record()}
            mock_ddb.return_value.Table.return_value.delete_item.side_effect = ClientError(
                {"Error": {"Code": "InternalServerError", "Message": "x"}}, "DeleteItem"
            )
            r = m.lambda_handler(_event(), MagicMock())
        assert r["statusCode"] == 500

    def test_unexpected_error_on_delete_returns_500(self):
        m = _load()
        with patch.object(m, "_ddb") as mock_ddb, \
             patch.object(m, "_delete_agent_user"):
            mock_ddb.return_value.Table.return_value.get_item.return_value = {"Item": _token_record()}
            mock_ddb.return_value.Table.return_value.delete_item.side_effect = RuntimeError("ddb down")
            r = m.lambda_handler(_event(), MagicMock())
        assert r["statusCode"] == 500


# ── Config error ──────────────────────────────────────────────────────────────
class TestConfigError:
    def test_missing_data_region_returns_500(self):
        original = os.environ.get("DATA_REGION")
        os.environ["DATA_REGION"] = ""
        try:
            m = _load()
            r = m.lambda_handler(_event(), MagicMock())
        finally:
            os.environ["DATA_REGION"] = original or "ap-south-1"
        assert r["statusCode"] == 500


# ── Response shape ────────────────────────────────────────────────────────────
class TestResponseShape:
    def test_cors_header_present(self):
        r, _, _, _ = _run_unlink(None)
        assert r["headers"]["Access-Control-Allow-Origin"] == "*"

    def test_content_type_is_json(self):
        r, _, _, _ = _run_unlink(None)
        assert r["headers"]["Content-Type"] == "application/json"

    def test_body_is_valid_json(self):
        r, _, _, _ = _run_unlink(None)
        assert isinstance(json.loads(r["body"]), dict)

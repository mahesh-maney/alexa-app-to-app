"""
Unit tests for alexa_callback/lambda_function.py

Run:
  cd /Users/maheshmaney/maney/digilux/app-to-app
  python -m pytest tests/test_alexa_callback.py -v
"""
import importlib.util
import os
import sys
import unittest
from urllib.parse import quote

_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SPEC = importlib.util.spec_from_file_location(
    "callback_fn",
    os.path.join(_HERE, "lambdas", "alexa_callback", "lambda_function.py"),
)
fn = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(fn)


def _event(code: str = None, state: str = None,
           error: str = None, error_description: str = None) -> dict:
    params = {}
    if code:               params["code"]              = code
    if state:              params["state"]             = state
    if error:              params["error"]             = error
    if error_description:  params["error_description"] = error_description
    return {"queryStringParameters": params or None}


class TestAlexaCallback(unittest.TestCase):

    # ── Success path ──────────────────────────────────────────────────────────

    def test_success_returns_200(self):
        resp = fn.lambda_handler(_event(code="AUTH123", state="STATE456"), {})
        self.assertEqual(resp["statusCode"], 200)

    def test_success_content_type_is_html(self):
        resp = fn.lambda_handler(_event(code="CODE", state="STATE"), {})
        self.assertIn("text/html", resp["headers"]["Content-Type"])

    def test_success_contains_deep_link(self):
        resp = fn.lambda_handler(_event(code="MYCODE", state="MYSTATE"), {})
        self.assertIn("digilux://alexa/callback", resp["body"])

    def test_success_deep_link_contains_code(self):
        resp = fn.lambda_handler(_event(code="MYCODE", state="MYSTATE"), {})
        self.assertIn("code=MYCODE", resp["body"])

    def test_success_deep_link_contains_state(self):
        resp = fn.lambda_handler(_event(code="MYCODE", state="MYSTATE"), {})
        self.assertIn("state=MYSTATE", resp["body"])

    def test_success_page_shows_connected_message(self):
        resp = fn.lambda_handler(_event(code="CODE", state="STATE"), {})
        self.assertIn("Alexa Connected", resp["body"])

    def test_success_page_has_open_app_button(self):
        resp = fn.lambda_handler(_event(code="CODE", state="STATE"), {})
        self.assertIn("Open Digilux App", resp["body"])

    def test_success_javascript_redirect_present(self):
        """Page must try to deep-link immediately via JS before showing button."""
        resp = fn.lambda_handler(_event(code="CODE", state="STATE"), {})
        self.assertIn("window.location.href", resp["body"])

    def test_special_characters_in_code_are_url_encoded(self):
        """Authorization codes may contain + or / — must be percent-encoded in deep link."""
        resp = fn.lambda_handler(_event(code="code+with/special=chars", state="STATE"), {})
        # Raw + should NOT appear in the deep link URL portion
        body = resp["body"]
        deep_link_start = body.find("digilux://alexa/callback")
        deep_link_end   = body.find('"', deep_link_start)
        deep_link       = body[deep_link_start:deep_link_end]
        self.assertNotIn("+", deep_link)
        self.assertNotIn("/", deep_link.split("?", 1)[1])  # only in query string part

    def test_special_characters_in_state_are_url_encoded(self):
        resp = fn.lambda_handler(_event(code="CODE", state="state with spaces"), {})
        body = resp["body"]
        deep_link_start = body.find("digilux://alexa/callback")
        deep_link_end   = body.find('"', deep_link_start)
        deep_link       = body[deep_link_start:deep_link_end]
        self.assertNotIn(" ", deep_link)

    # ── Error path ────────────────────────────────────────────────────────────

    def test_error_access_denied_returns_200(self):
        """Error page still returns 200 — it's a user-facing HTML page, not an API error."""
        resp = fn.lambda_handler(_event(error="access_denied"), {})
        self.assertEqual(resp["statusCode"], 200)

    def test_error_page_shows_linking_failed(self):
        resp = fn.lambda_handler(_event(error="access_denied"), {})
        self.assertIn("Linking Failed", resp["body"])

    def test_error_page_shows_error_code(self):
        resp = fn.lambda_handler(_event(error="access_denied"), {})
        self.assertIn("access_denied", resp["body"])

    def test_error_page_shows_description_when_present(self):
        resp = fn.lambda_handler(
            _event(error="access_denied", error_description="User cancelled"), {}
        )
        self.assertIn("User cancelled", resp["body"])

    def test_error_page_has_return_to_app_button(self):
        resp = fn.lambda_handler(_event(error="access_denied"), {})
        self.assertIn("Return to App", resp["body"])

    def test_error_page_deep_link_carries_error(self):
        resp = fn.lambda_handler(_event(error="access_denied"), {})
        self.assertIn("digilux://alexa/callback", resp["body"])
        self.assertIn("error=access_denied", resp["body"])

    def test_server_error_code_included_in_error_page(self):
        resp = fn.lambda_handler(_event(error="server_error"), {})
        self.assertIn("server_error", resp["body"])

    # ── Missing / invalid params ───────────────────────────────────────────────

    def test_missing_code_returns_error_page(self):
        resp = fn.lambda_handler(_event(state="STATE"), {})
        self.assertIn("Linking Failed", resp["body"])

    def test_missing_state_returns_error_page(self):
        resp = fn.lambda_handler(_event(code="CODE"), {})
        self.assertIn("Linking Failed", resp["body"])

    def test_no_params_at_all_returns_error_page(self):
        resp = fn.lambda_handler({"queryStringParameters": None}, {})
        self.assertIn("Linking Failed", resp["body"])

    def test_empty_params_dict_returns_error_page(self):
        resp = fn.lambda_handler({"queryStringParameters": {}}, {})
        self.assertIn("Linking Failed", resp["body"])

    # ── Error takes precedence over code ──────────────────────────────────────

    def test_error_param_takes_priority_over_code(self):
        """If both error and code are present, show the error page."""
        resp = fn.lambda_handler(
            _event(code="SOME_CODE", state="STATE", error="access_denied"), {}
        )
        self.assertIn("Linking Failed", resp["body"])
        self.assertNotIn("Alexa Connected", resp["body"])

    # ── HTML structure ────────────────────────────────────────────────────────

    def test_success_page_is_valid_html(self):
        resp = fn.lambda_handler(_event(code="C", state="S"), {})
        body = resp["body"]
        self.assertIn("<!DOCTYPE html>", body)
        self.assertIn("<html", body)
        self.assertIn("</html>", body)
        self.assertIn("<head>", body)
        self.assertIn("<body>", body)

    def test_error_page_is_valid_html(self):
        resp = fn.lambda_handler(_event(error="access_denied"), {})
        body = resp["body"]
        self.assertIn("<!DOCTYPE html>", body)
        self.assertIn("</html>", body)

    def test_success_page_has_viewport_meta(self):
        """Must be mobile-friendly."""
        resp = fn.lambda_handler(_event(code="C", state="S"), {})
        self.assertIn('name="viewport"', resp["body"])

    def test_success_page_has_delayed_button_display(self):
        """Button starts hidden (style=display:none) and appears after 2s via JS."""
        resp = fn.lambda_handler(_event(code="C", state="S"), {})
        self.assertIn("display:none", resp["body"])
        self.assertIn("setTimeout", resp["body"])

    def test_app_scheme_matches_constant(self):
        resp = fn.lambda_handler(_event(code="C", state="S"), {})
        self.assertIn(fn._APP_SCHEME + "://", resp["body"])


if __name__ == "__main__":
    unittest.main(verbosity=2)

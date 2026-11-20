"""
E2E tests — Alexa smart home fulfillment Lambda.

Invoked directly via boto3 (not via API Gateway) — matches production behaviour
where Amazon invokes the Lambda ARN directly.

Covers:
  - Discover → returns non-empty endpoint list
  - TurnOn / TurnOff → returns SUCCESS response
  - Invalid LWA token → returns EXPIRED_AUTHORIZATION_CREDENTIAL

Requires:
  - TEST_ALEXA_LWA_TOKEN env var (LWA access token from a linked account)
  - TEST_DEVICE_ID env var (Alexa endpoint ID to target)
  - AWS credentials with lambda:InvokeFunction on the fulfillment ARN
"""
from __future__ import annotations

import json
import os
import uuid

import boto3
import pytest


pytestmark = pytest.mark.fulfillment


@pytest.fixture(scope="module")
def lwa_token():
    token = os.getenv("TEST_ALEXA_LWA_TOKEN", "")
    if not token:
        pytest.skip(
            "TEST_ALEXA_LWA_TOKEN not set. "
            "Complete the Alexa app-to-app link flow, then set the LWA access token."
        )
    return token


@pytest.fixture(scope="module")
def alexa_lambda(cfg):
    return boto3.client("lambda", region_name=cfg.alexa_fulfillment_region)


def _invoke(alexa_lambda, fulfillment_arn: str, directive: dict) -> dict:
    resp = alexa_lambda.invoke(
        FunctionName=fulfillment_arn,
        InvocationType="RequestResponse",
        Payload=json.dumps(directive).encode(),
    )
    return json.loads(resp["Payload"].read())


def _header(namespace: str, name: str, msg_id: str = None) -> dict:
    return {
        "namespace":        namespace,
        "name":             name,
        "payloadVersion":   "3",
        "messageId":        msg_id or str(uuid.uuid4()),
    }


def _scope(token: str) -> dict:
    return {"type": "BearerToken", "token": token}


class TestAlexaFulfillmentAuth:

    def test_invalid_token_returns_expired_auth_credential(self, cfg, alexa_lambda):
        directive = {
            "directive": {
                "header":   _header("Alexa.Discovery", "Discover"),
                "payload":  {"scope": _scope("invalid.lwa.token")},
            }
        }
        result = _invoke(alexa_lambda, cfg.alexa_fulfillment_arn, directive)
        # Alexa spec: invalid token returns ErrorResponse with EXPIRED_AUTHORIZATION_CREDENTIAL
        assert result["event"]["header"]["namespace"] == "Alexa", (
            f"Unexpected namespace: {result}"
        )
        assert result["event"]["payload"]["type"] == "EXPIRED_AUTHORIZATION_CREDENTIAL", (
            f"Expected EXPIRED_AUTHORIZATION_CREDENTIAL, got: {result['event']['payload']}"
        )


class TestAlexaDiscover:

    def test_discover_returns_endpoints(self, cfg, alexa_lambda, lwa_token):
        directive = {
            "directive": {
                "header":  _header("Alexa.Discovery", "Discover"),
                "payload": {"scope": _scope(lwa_token)},
            }
        }
        result = _invoke(alexa_lambda, cfg.alexa_fulfillment_arn, directive)
        assert result["event"]["header"]["namespace"] == "Alexa.Discovery", (
            f"Unexpected response: {result}"
        )
        assert result["event"]["header"]["name"] == "Discover.Response"
        endpoints = result["event"]["payload"]["endpoints"]
        assert len(endpoints) > 0, "Discover returned no endpoints"

    def test_discover_endpoints_have_required_fields(self, cfg, alexa_lambda, lwa_token):
        directive = {
            "directive": {
                "header":  _header("Alexa.Discovery", "Discover"),
                "payload": {"scope": _scope(lwa_token)},
            }
        }
        result = _invoke(alexa_lambda, cfg.alexa_fulfillment_arn, directive)
        for ep in result["event"]["payload"]["endpoints"]:
            assert "endpointId"   in ep, f"Missing endpointId: {ep}"
            assert "friendlyName" in ep, f"Missing friendlyName: {ep}"
            assert "capabilities" in ep, f"Missing capabilities: {ep}"


class TestAlexaPowerController:

    def test_turn_on_returns_success(self, cfg, alexa_lambda, lwa_token, device_id):
        directive = {
            "directive": {
                "header":   _header("Alexa.PowerController", "TurnOn",
                                    msg_id=str(uuid.uuid4())),
                "endpoint": {
                    "endpointId": device_id,
                    "scope":      _scope(lwa_token),
                },
                "payload": {},
            }
        }
        result = _invoke(alexa_lambda, cfg.alexa_fulfillment_arn, directive)
        assert result["event"]["header"]["name"] == "Response", (
            f"TurnOn did not return Response: {result}"
        )
        assert result["event"]["header"]["namespace"] == "Alexa"

    def test_turn_off_returns_success(self, cfg, alexa_lambda, lwa_token, device_id):
        directive = {
            "directive": {
                "header":   _header("Alexa.PowerController", "TurnOff",
                                    msg_id=str(uuid.uuid4())),
                "endpoint": {
                    "endpointId": device_id,
                    "scope":      _scope(lwa_token),
                },
                "payload": {},
            }
        }
        result = _invoke(alexa_lambda, cfg.alexa_fulfillment_arn, directive)
        assert result["event"]["header"]["name"] == "Response"

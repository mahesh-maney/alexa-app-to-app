"""
tests/e2e/config.py — Environment configuration for E2E tests.

Select environment via TEST_ENV env var (default: digilux-qa).

Required env vars (set in .env.e2e or export before running):
    TEST_USER_EMAIL       Cognito test user email
    TEST_USER_PASSWORD    Cognito test user password
    TEST_SITE_ID          A site ID that belongs to the test user
    TEST_DEVICE_ID        A device ID in that site (for fulfillment tests)

Optional:
    TEST_GH_ACCESS_TOKEN   Pre-obtained Google Home OAuth access token
                           (skips browser-based GH OAuth flow for fulfillment tests)
    TEST_ALEXA_LWA_TOKEN   Pre-obtained Alexa LWA access token
                           (skips browser-based Alexa LWA flow for fulfillment tests)
    TEST_AMAZON_EMAIL      Amazon account email (for Alexa app-to-app LWA login)
    TEST_AMAZON_PASSWORD   Amazon account password
"""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class EnvConfig:
    # API
    api_base: str               # e.g. https://iot.digilux.co.in/smarthome
    # Cognito
    cognito_region: str
    cognito_user_pool_id: str
    cognito_client_id: str
    cognito_domain: str         # e.g. auth.digilux.co.in
    # Alexa fulfillment Lambda (direct invoke)
    alexa_fulfillment_arn: str
    alexa_fulfillment_region: str
    # Google Home
    gh_client_id: str
    gh_redirect_uri: str
    # AWS region for boto3
    aws_region: str


_ENVS: dict[str, EnvConfig] = {
    "digilux-qa": EnvConfig(
        api_base                = "https://iot.digilux.co.in/smarthome",
        cognito_region          = "ap-south-1",
        cognito_user_pool_id    = "ap-south-1_h1o8s7257",
        cognito_client_id       = "q7189jitfkk4ttesepkgls491",
        cognito_domain          = "auth.digilux.co.in",
        alexa_fulfillment_arn   = "arn:aws:lambda:eu-west-1:986906626244:function:alexa_fulfillment",
        alexa_fulfillment_region= "eu-west-1",
        gh_client_id            = "75gmuum6qhfjsedrvck809oie6",
        gh_redirect_uri         = "https://oauth-redirect.googleusercontent.com/r/digilux-ai-pro",
        aws_region              = "ap-south-1",
    ),
    "honeywell-qa": EnvConfig(
        api_base                = "https://api.nucleusqa.honeywell.com",
        cognito_region          = "us-east-1",
        cognito_user_pool_id    = "us-east-1_DuegVptxS",
        cognito_client_id       = "43jurrip0q1lrhbl1i6pgs74po",
        cognito_domain          = "auth.nucleusqa.honeywell.com",
        alexa_fulfillment_arn   = "arn:aws:lambda:eu-west-1:316336725141:function:alexa_fulfillment",
        alexa_fulfillment_region= "eu-west-1",
        gh_client_id            = "3her30asrou79v3jnt9nj5i22h",
        gh_redirect_uri         = "https://oauth-redirect.googleusercontent.com/r/honeywell-nucleus",
        aws_region              = "us-east-1",
    ),
}


def get_config() -> EnvConfig:
    env = os.getenv("TEST_ENV", "digilux-qa")
    if env not in _ENVS:
        raise ValueError(f"Unknown TEST_ENV={env!r}. Valid values: {list(_ENVS)}")
    return _ENVS[env]


# Credentials — always from env vars, never hardcoded
def test_user_email()    -> str: return _require("TEST_USER_EMAIL")
def test_user_password() -> str: return _require("TEST_USER_PASSWORD")
def test_site_id()       -> str: return _require("TEST_SITE_ID")
def test_device_id()     -> str: return os.getenv("TEST_DEVICE_ID", "")
def test_amazon_email()  -> str: return os.getenv("TEST_AMAZON_EMAIL", "")
def test_amazon_password()-> str: return os.getenv("TEST_AMAZON_PASSWORD", "")
def gh_access_token_override() -> str: return os.getenv("TEST_GH_ACCESS_TOKEN", "")
def alexa_lwa_token_override() -> str: return os.getenv("TEST_ALEXA_LWA_TOKEN", "")
def gh_client_secret()   -> str: return _require("TEST_GH_CLIENT_SECRET")


def _require(var: str) -> str:
    val = os.getenv(var)
    if not val:
        raise EnvironmentError(
            f"Required env var {var!r} is not set. "
            f"Set it in .env.e2e or export it before running pytest."
        )
    return val

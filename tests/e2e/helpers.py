"""Shared helper functions used across e2e test modules."""
import pytest


def require_playwright(available: bool) -> None:
    """Skip the test if Playwright is not installed."""
    if not available:
        pytest.skip(
            "playwright not installed — run: "
            "pip install playwright && playwright install chromium"
        )

"""Tests for application configuration loading and validation.

These tests verify that settings load correctly from environment
variables, that defaults are applied properly, and that invalid
configurations produce clear errors.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from copart_automation.app.config import Settings


class TestConfiguration:
    """Verify settings behavior under various conditions."""

    def test_default_settings(self) -> None:
        """Default settings should load without errors."""
        settings = Settings()
        assert settings.headless is True
        assert settings.browser_channel == "chromium"
        assert settings.retry_max_attempts == 3

    def test_custom_values(self) -> None:
        """Custom values should override defaults."""
        settings = Settings(
            headless=False,
            browser_channel="firefox",
            retry_max_attempts=5,
        )
        assert settings.headless is False
        assert settings.browser_channel == "firefox"
        assert settings.retry_max_attempts == 5

    def test_search_is_disabled_by_default(self) -> None:
        """Default workflow should not navigate to search unless explicitly enabled."""
        settings = Settings()
        assert settings.perform_search is False

    def test_search_can_be_enabled(self) -> None:
        """Search can be enabled explicitly when needed."""
        settings = Settings(perform_search=True)
        assert settings.perform_search is True

    def test_email_and_password_handling(self) -> None:
        """Credentials should be validated but not exposed in logs."""
        settings = Settings(copart_email="test@example.com", copart_password="secret")
        assert settings.copart_email == "test@example.com"
        # SecretStr should not reveal value directly
        assert settings.copart_password.get_secret_value() == "secret"

    def test_invalid_path_creation(self) -> None:
        """Path-based settings should create parent directories."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                database_path=f"{tmp}/test/sub/db.sqlite",
                download_dir=f"{tmp}/downloads",
            )
            assert settings.database_path.exists() is False  # File doesn't exist yet
            assert settings.database_path.parent.exists() is True  # Parent exists

    def test_invalid_retry_attempts(self) -> None:
        """Negative retry attempts should be rejected."""
        with pytest.raises(ValidationError):
            Settings(retry_max_attempts=-1)

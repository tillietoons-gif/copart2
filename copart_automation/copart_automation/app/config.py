"""Application configuration using Pydantic Settings and python-dotenv.

Design decisions:
- Pydantic Settings validates all environment variables at import time,
  providing immediate, clear error messages for misconfiguration.
- Sensitive values (email, password) are never logged or serialized.
- Default paths are relative to the project root to support portable
  deployment and testing environments.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import ConfigDict, Field, SecretStr
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Validated application settings loaded from environment variables.

    Fields use sensible defaults to allow the application to start
    without a .env file (though credentials will be missing and
    login will fail gracefully).
    """

    # Copart account credentials (required for authentication)
    copart_email: str = Field(default="", description="Copart account email address")
    copart_password: SecretStr = Field(
        default=SecretStr(""), description="Copart account password"
    )

    # Browser configuration
    headless: bool = Field(default=True, description="Run browser in headless mode")
    browser_channel: str = Field(default="chromium", description="Playwright browser channel")
    perform_search: bool = Field(default=False, description="Perform the demo search step in the workflow")

    # File paths
    download_dir: Path = Field(default=Path("downloads"), description="Download directory")
    database_path: Path = Field(default=Path("data/copart.db"), description="SQLite database file")
    storage_state_path: Path = Field(
        default=Path("data/auth_state.json"), description="Playwright storage state file"
    )

    # Timeouts (milliseconds, mapped to Playwright expectations)
    navigation_timeout: int = Field(default=30000, description="Page navigation timeout (ms)")
    action_timeout: int = Field(default=10000, description="Individual action timeout (ms)")

    # Retry configuration
    retry_max_attempts: int = Field(default=3, ge=1, description="Max retry attempts for transient errors")
    retry_backoff_multiplier: float = Field(default=2.0, description="Exponential backoff multiplier")
    retry_initial_delay: float = Field(default=1.0, description="Initial retry delay (seconds)")

    # Logging
    log_level: str = Field(default="INFO", description="Minimum log level")
    log_file: str = Field(default="logs/copart_automation.log", description="Log file path")
    log_rotation: str = Field(default="10 MB", description="Log rotation size")
    log_retention: str = Field(default="7 days", description="Log retention period")

    model_config = ConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    def __init__(self, **data) -> None:  # noqa: ANN003
        # Resolve paths relative to project root before creating dirs
        root = Path(__file__).resolve().parent.parent.parent
        for attr in ("download_dir", "database_path", "storage_state_path"):
            if attr in data:
                path_val = data[attr]
                if isinstance(path_val, (str, Path)):
                    path_obj = Path(path_val)
                    if not path_obj.is_absolute():
                        data[attr] = root / path_obj
        super().__init__(**data)
        # Ensure directories exist for path-based settings
        self.download_dir.mkdir(parents=True, exist_ok=True)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.storage_state_path.parent.mkdir(parents=True, exist_ok=True)


# Global settings instance
# Importing this triggers validation; import errors indicate bad .env/config.
settings = Settings()

# src/hermes/config.py
"""
Centralized configuration for Hermes.
- Loads environment variables (via .env in dev) using Pydantic BaseSettings
- Provides typed settings with sane defaults
- Exposes a singleton `settings` for convenience, plus `get_settings()` for DI

Add these deps in your pyproject:
[project.optional-dependencies]
hermes = [
  "pydantic>=2.7",
  "python-dotenv>=1.0",  # Pydantic will auto-read .env if configured
]
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings


ROOT = Path(__file__).resolve().parents[2]  # repo root (…/src/hermes/ → …/)
DEFAULT_DATA_DIR = ROOT / "data"
DEFAULT_CREDENTIALS_DIR = ROOT / ".credentials"


class GoogleOAuthPaths(BaseModel):
    client_secrets_path: Path = Field(default=DEFAULT_CREDENTIALS_DIR / "credentials.json")
    token_path: Path = Field(default=DEFAULT_CREDENTIALS_DIR / "token.json")

    def ensure_dirs(self) -> None:
        self.client_secrets_path.parent.mkdir(parents=True, exist_ok=True)
        self.token_path.parent.mkdir(parents=True, exist_ok=True)


class Settings(BaseSettings):
    """Typed, validated app configuration.

    Usage:
        from hermes.config import settings
        db_path = settings.db_path
    """

    # --- runtime environment ---
    env: Literal["local", "test", "prod"] = Field(default="local", description="Runtime environment")

    # --- database ---
    data_dir: Path = Field(default=DEFAULT_DATA_DIR)
    db_filename: str = Field(default="hermes.db")

    # --- providers: Google ---
    google: GoogleOAuthPaths = Field(default_factory=GoogleOAuthPaths)
    gmail_scopes: tuple[str, ...] = (
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/gmail.modify",
        "https://www.googleapis.com/auth/gmail.send",
    )
    gcal_scopes: tuple[str, ...] = (
        "https://www.googleapis.com/auth/calendar.readonly",
        "https://www.googleapis.com/auth/calendar.events",
        "https://www.googleapis.com/auth/calendar",
    )

    # --- providers: Google Gmail ---
    gmail_api_version: str = "v1"
    gmail_user_id: str = "me"

    # --- providers: Google Calendar ---
    gcal_api_version: str = "v3"
    gcal_calendar_id: str = "primary"

    # --- shared OAuth + app defaults ---
    oauth_headless: bool = False
    oauth_port: int = 0           # 0 lets Google pick an open port
    app_user_agent: str = "hermes/0.1"


    # --- providers: OpenAI / LLM ---
    openai_api_key: SecretStr | None = Field(default=None)
    llm_model: str | None = Field(default="gpt-4o-mini")
    llm_timeout_s: int = 30

    # --- logging ---
    log_level: str = Field(default="INFO")
    log_json: bool = Field(default=True, description="Emit logs as JSON if True, pretty if False")
    log_dir: Path = Field(default=DEFAULT_DATA_DIR / "logs")
    log_file: str = Field(default="hermes.log")
    log_max_bytes: int = Field(default=2 * 1024 * 1024)  # 2MB
    log_backup_count: int = Field(default=3)
    redact_emails_in_logs: bool = Field(default=True)

    # --- app defaults ---
    timezone: str = Field(default="America/Denver")

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        case_sensitive = False

    @property
    def db_path(self) -> Path:
        return self.data_dir / self.db_filename

    @property
    def log_path(self) -> Path:
        return self.log_dir / self.log_file

    @field_validator("data_dir", "log_dir")
    @classmethod
    def _expand_user(cls, v: Path) -> Path:
        return Path(os.path.expanduser(str(v)))

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.google.ensure_dirs()


# Singleton-ish settings instance for convenience
settings = Settings()
settings.ensure_dirs()


def get_settings() -> Settings:
    """Factory to retrieve settings (handy for dependency injection in tests)."""
    return settings
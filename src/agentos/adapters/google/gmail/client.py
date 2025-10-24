# src/agentos/adapters/google/gmail/client.py

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence, Any, cast

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# Project logging & config
from agentos.logging_utils import get_logger
from agentos.config import settings  # single source of truth

log = get_logger(__name__)


# ---------- Defaults (from centralized settings) ----------
DEFAULT_SCOPES: Sequence[str] = settings.gmail_scopes
CRED_FILE: Path = settings.google.client_secrets_path
TOKEN_FILE: Path = settings.google.token_path

GMAIL_API_VERSION: str = settings.gmail_api_version
GMAIL_USER_ID: str = settings.gmail_user_id
OAUTH_HEADLESS: bool = settings.oauth_headless
OAUTH_PORT: int = settings.oauth_port
APP_USER_AGENT: str = settings.app_user_agent


@dataclass
class GmailClientConfig:
    """
    Configuration for GmailClient. Values are supplied explicitly or read from Settings.
    """
    scopes: Sequence[str] = tuple(DEFAULT_SCOPES)
    credentials_file: Path = CRED_FILE
    token_file: Path = TOKEN_FILE
    api_name: str = "gmail"
    api_version: str = GMAIL_API_VERSION
    user_id: str = GMAIL_USER_ID
    headless: bool = OAUTH_HEADLESS
    local_server_port: int = OAUTH_PORT
    user_agent: str = APP_USER_AGENT

    @classmethod
    def from_settings_or_env(cls) -> "GmailClientConfig":
        # Intentionally only reads from centralized settings (no direct env here).
        return cls(
            scopes=tuple(settings.gmail_scopes),
            credentials_file=settings.google.client_secrets_path,
            token_file=settings.google.token_path,
            api_version=settings.gmail_api_version,
            user_id=settings.gmail_user_id,
            headless=bool(settings.oauth_headless),
            local_server_port=int(settings.oauth_port),
            user_agent=settings.app_user_agent,
        )


class GmailClient:
    """
    Lowest-level Gmail API client.
    - Owns OAuth 2.0: load/refresh tokens, first-run consent.
    - Builds and returns an authenticated discovery `service`.
    - Surfaces raw API resources; higher layers (reader/writer) add logic.
    """

    def __init__(self, config: Optional[GmailClientConfig] = None) -> None:
        self.config = config or GmailClientConfig.from_settings_or_env()
        self._service = None

        # Ensure credential directory exists
        self.config.token_file.parent.mkdir(parents=True, exist_ok=True)

    # ---------- Public API ----------

    def get_service(self):
        """Return an authenticated Gmail discovery service. Lazily creates it."""
        if self._service is None:
            creds = self._get_credentials()
            # Build service
            try:
                self._service = build(
                    self.config.api_name,
                    self.config.api_version,
                    credentials=creds,
                    cache_discovery=False,
                )
                # Set custom user-agent on underlying http if present
                try:
                    http = self._service._http  # type: ignore[attr-defined]
                    http.headers["user-agent"] = self.config.user_agent
                except Exception:
                    pass
                log.info(
                    "gmail.client.service_built",
                    extra={
                        "api": self.config.api_name,
                        "version": self.config.api_version,
                        "scopes": list(self.config.scopes),
                    },
                )
            except HttpError:
                log.exception("gmail.client.service_build_failed")
                raise
        return self._service

    def raw(self):
        """Convenience: return the raw discovery service (same as get_service())."""
        return self.get_service()

    # ---------- Internals ----------

    def _get_credentials(self) -> Credentials:
        """
        Load credentials from token.json, refresh if needed, or run OAuth flow to obtain new credentials.
        Saves updated token.json when credentials are created or refreshed.
        """
        creds: Optional[Credentials] = None

        # 1) Load existing token if present
        if self.config.token_file.exists():
            try:
                creds = Credentials.from_authorized_user_file(
                    str(self.config.token_file), list(self.config.scopes)
                )
            except Exception:
                log.warning(
                    "gmail.client.token_load_failed",
                    extra={"token_file": str(self.config.token_file)},
                )
                creds = None

        # 2) Refresh if expired and refresh_token present
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                self._persist_token(creds)
                log.info("gmail.client.token_refreshed")
                return creds
            except Exception:
                log.warning("gmail.client.token_refresh_failed")
                creds = None  # fall through to new flow

        # 3) If no valid creds, run OAuth flow
        if not creds or not creds.valid:
            if not self.config.credentials_file.exists():
                raise FileNotFoundError(
                    f"Client secrets not found at {self.config.credentials_file}. "
                    "Download OAuth client (Desktop) JSON and place it there."
                )

            flow = InstalledAppFlow.from_client_secrets_file(
                str(self.config.credentials_file), list(self.config.scopes)
            )

            # The typeshed for google-auth sometimes reports a union return that confuses
            # static type checkers. Cast to Any for the call and then narrow to the
            # expected Credentials type with an assertion so both static analysis and
            # runtime consumers are satisfied.
            flow_any: Any = flow

            if self.config.headless:
                # For headless servers (copy/paste code)
                log.info("gmail.client.oauth_console_flow_started")
                creds_any = flow_any.run_console()
            else:
                # Local server flow opens browser; port=0 lets it choose a free port
                log.info("gmail.client.oauth_local_server_flow_started", extra={"port": self.config.local_server_port})
                creds_any = flow_any.run_local_server(port=self.config.local_server_port)

            creds = cast(Credentials, creds_any)
            # runtime check to satisfy linters/typers
            assert creds is not None and isinstance(creds, Credentials)

            self._persist_token(creds)
            log.info("gmail.client.token_obtained")

        return creds

    def _persist_token(self, creds: Credentials) -> None:
        """Write refreshed/new credentials to token.json with minimal fields."""
        payload = {
            "token": creds.token,
            "refresh_token": getattr(creds, "refresh_token", None),
            "token_uri": getattr(creds, "token_uri", None),
            "client_id": getattr(creds, "client_id", None),
            "client_secret": getattr(creds, "client_secret", None),
            "scopes": list(creds.scopes or self.config.scopes),
        }
        with self.config.token_file.open("w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        try:
            os.chmod(self.config.token_file, 0o600)
        except Exception:
            pass


# ---------- Convenience factory ----------

def get_gmail_service(config: Optional[GmailClientConfig] = None):
    """
    One-liner for callers that just need the discovery service.
    Example:
        service = get_gmail_service()
        service.users().threads().list(userId="me").execute()
    """
    return GmailClient(config=config).get_service()

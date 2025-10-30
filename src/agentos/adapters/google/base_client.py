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

from agentos.logging_utils import get_logger
from agentos.config import settings

log = get_logger(__name__)


# ---------- Shared Config ----------

@dataclass
class GoogleClientConfig:
    """
    Generic configuration for Google API clients (Gmail, Calendar, etc.).
    Subclasses should override `api_name`, `api_version`, and `scopes`.
    """
    scopes: Sequence[str]
    credentials_file: Path
    token_file: Path
    api_name: str
    api_version: str
    user_id: str
    headless: bool
    local_server_port: int
    user_agent: str

    @classmethod
    def from_settings(cls, *, scopes: Sequence[str], api_name: str, api_version: str, user_id: str) -> GoogleClientConfig:
        """Factory to create a config for any Google API from central settings."""
        return cls(
            scopes=scopes,
            credentials_file=settings.google.client_secrets_path,
            token_file=settings.google.token_path,
            api_name=api_name,
            api_version=api_version,
            user_id=user_id,
            headless=bool(settings.oauth_headless),
            local_server_port=int(settings.oauth_port),
            user_agent=settings.app_user_agent,
        )


# ---------- Base Client ----------

class GoogleClient:
    """
    Base class for Google API clients (Gmail, Calendar, etc.).
    Handles OAuth 2.0 authorization, token refresh, and authenticated service creation.
    Subclasses should set `config.api_name` appropriately before calling get_service().
    """

    def __init__(self, config: GoogleClientConfig) -> None:
        self.config = config
        self._service = None

        # Ensure credential directory exists
        settings.google.ensure_dirs()

    # ---------- Public API ----------

    def get_service(self):
        """Return an authenticated discovery service. Lazily creates it."""
        if self._service is None:
            creds = self._get_credentials()
            try:
                self._service = build(
                    self.config.api_name,
                    self.config.api_version,
                    credentials=creds,
                    cache_discovery=False,
                )
                # Attach user-agent if possible
                try:
                    http = self._service._http  # type: ignore[attr-defined]
                    http.headers["user-agent"] = self.config.user_agent
                except Exception:
                    pass

                log.info(
                    "google.client.service_built",
                    extra={
                        "api": self.config.api_name,
                        "version": self.config.api_version,
                        "scopes": list(self.config.scopes),
                    },
                )
            except HttpError:
                log.exception("google.client.service_build_failed")
                raise
        return self._service

    def raw(self):
        """Alias for get_service()."""
        return self.get_service()

    # ---------- Internals ----------

    def _get_credentials(self) -> Credentials:
        """Load credentials from token.json, refresh if needed, or run OAuth flow to obtain new credentials."""
        creds: Optional[Credentials] = None

        # Load existing token if present and there is something passed in for the path
        if self.config.token_file.exists():
            try:
                creds = Credentials.from_authorized_user_file(
                    str(self.config.token_file), list(self.config.scopes)
                )
            except Exception:
                log.warning(
                    "google.client.token_load_failed",
                    extra={"token_file": str(self.config.token_file)},
                )
                creds = None

        # Refresh if expired
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                self._persist_token(creds)
                log.info("google.client.token_refreshed")
                return creds
            except Exception:
                log.warning("google.client.token_refresh_failed")
                creds = None

        # Run OAuth flow if no valid credentials
        if not creds or not creds.valid:
            if not self.config.credentials_file.exists():
                raise FileNotFoundError(
                    f"Client secrets not found at {self.config.credentials_file}. "
                    "Download OAuth client (Desktop) JSON and place it there."
                )

            flow = InstalledAppFlow.from_client_secrets_file(
                str(self.config.credentials_file), list(self.config.scopes)
            )

            flow_any: Any = flow

            if self.config.headless:
                log.info("google.client.oauth_console_flow_started")
                creds_any = flow_any.run_console()
            else:
                log.info(
                    "google.client.oauth_local_server_flow_started",
                    extra={"port": self.config.local_server_port},
                )
                creds_any = flow_any.run_local_server(port=self.config.local_server_port)

            creds = cast(Credentials, creds_any)
            assert creds is not None and isinstance(creds, Credentials)

            self._persist_token(creds)
            log.info("google.client.token_obtained")

        return creds

    def _persist_token(self, creds: Credentials) -> None:
        """Persist refreshed/new credentials to token.json."""
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

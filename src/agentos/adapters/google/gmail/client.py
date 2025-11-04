from __future__ import annotations
from typing import Optional

from agentos.adapters.google.base_client import GoogleClient, GoogleClientConfig
from agentos.config import settings


class GmailClient(GoogleClient):
    """Google Gmail API client built on the shared GoogleClient base."""

    @classmethod
    def from_settings(cls) -> GmailClient:
        config = GoogleClientConfig.from_settings(
            scopes=tuple(settings.gmail_scopes),
            api_name="gmail",
            api_version=settings.gmail_api_version,
            user_id=settings.gmail_user_id,
        )
        return cls(config)

    def __init__(self, config: Optional[GoogleClientConfig] = None) -> None:
        super().__init__(config or self.from_settings().config)


# ---------- Convenience Factory ----------

def get_gmail_service(config: Optional[GoogleClientConfig] = None):
    """
    One-liner for callers that just need the discovery service.
    Example:
        service = get_gmail_service()
        service.users().threads().list(userId="me").execute()
    """
    return GmailClient(config=config or GmailClient.from_settings().config).get_service()

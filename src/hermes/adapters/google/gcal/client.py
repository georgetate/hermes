"""Google Calendar API client wrapper built on `GoogleClient`."""

from __future__ import annotations
from typing import Optional

from hermes.adapters.google.base_client import GoogleClient, GoogleClientConfig
from hermes.config import settings


class GCalClient(GoogleClient):
    """Google Calendar API client built on the shared GoogleClient base."""

    @classmethod
    def from_settings(cls) -> GCalClient:
        """Build a `GCalClient` using configured Calendar API settings."""
        config = GoogleClientConfig.from_settings(
            scopes=tuple(settings.gcal_scopes),
            api_name="calendar",
            api_version=settings.gcal_api_version,
            user_id="primary",
        )
        return cls(config)

    def __init__(self, config: Optional[GoogleClientConfig] = None) -> None:
        super().__init__(config or self.from_settings().config)


# ---------- Convenience Factory ----------

def get_calendar_service(config: Optional[GoogleClientConfig] = None):
    """
    One-liner for callers that just need the discovery service.
    Example:
        service = get_calendar_service()
        service.events().list(calendarId="primary").execute()
    """
    return GCalClient(config=config or GCalClient.from_settings().config).get_service()

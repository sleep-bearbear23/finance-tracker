"""Environment configuration + shared time helpers."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    DATABASE_URL: str
    ANTHROPIC_API_KEY: str
    ANTHROPIC_MODEL: str = "claude-sonnet-5"

    LINE_CHANNEL_ACCESS_TOKEN: str
    LINE_CHANNEL_SECRET: str

    SIMPLEFIN_SETUP_TOKEN: str = ""
    SIMPLEFIN_ACCESS_URL: str = ""

    # Shared secret for the iOS Shortcut -> /ingest/tap endpoint (Apple Card real-time)
    INGEST_TOKEN: str = ""

    # Secret that unlocks the web dashboard (empty = dashboard disabled)
    DASHBOARD_TOKEN: str = ""

    TIMEZONE: str = "America/Los_Angeles"
    POLL_INTERVAL_MIN: int = 15
    DEBOUNCE_MINUTES: int = 5
    BACKFILL_DAYS: int = 45
    REMINDER_HOUR: int = 21  # local hour she nudges "send me today's screenshot"


settings = Settings()  # reads env / .env
TZ = ZoneInfo(settings.TIMEZONE)


def now() -> datetime:
    """Timezone-aware 'now' in the user's local zone."""
    return datetime.now(TZ)


def aware(dt: datetime | None) -> datetime | None:
    """Coerce a possibly-naive datetime to tz-aware, so date math never crashes."""
    if dt is None:
        return None
    return dt.replace(tzinfo=TZ) if dt.tzinfo is None else dt

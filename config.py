"""
Central configuration loaded from environment variables / .env file.
Never log or print any of these values — they contain secrets.

Modes
-----
MODE=manual   Starts with only OPENAI_API_KEY, OPENAI_MODEL, and ACCESS_PASSWORD.
              All Strava variables are ignored / optional.

MODE=webhook  Full Strava integration. All STRAVA_* vars and ATHLETE_ID are required.
"""

from functools import lru_cache
from typing import Optional

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # ── Mode ──────────────────────────────────────────────────────────────────
    # "manual"  → web form + OpenAI only; no Strava required
    # "webhook" → full automatic Strava integration (default)
    mode: str = "webhook"

    # ── Strava OAuth (required only when MODE=webhook) ─────────────────────────
    strava_client_id: Optional[str] = None
    strava_client_secret: Optional[str] = None
    strava_refresh_token: Optional[str] = None
    strava_verify_token: Optional[str] = None
    athlete_id: Optional[int] = None

    # Strava API base URL.
    # UPDATE to https://api-v3.strava.com/api/v3 after January 4, 2027.
    strava_base_url: str = "https://www.strava.com/api/v3"

    # ── OpenAI (required in both modes) ───────────────────────────────────────
    openai_api_key: str
    openai_model: str = "gpt-4o-mini"

    # ── Manual mode ───────────────────────────────────────────────────────────
    # Password for the web UI. Required when MODE=manual.
    # In webhook mode it's optional — if blank, the UI skips the password gate.
    access_password: str = ""

    # ── App behaviour ─────────────────────────────────────────────────────────
    dry_run: bool = False
    db_path: str = "run_titler.db"

    @model_validator(mode="after")
    def _validate_per_mode(self) -> "Settings":
        if self.mode not in ("manual", "webhook"):
            raise ValueError("MODE must be 'manual' or 'webhook'")

        if self.mode == "webhook":
            missing = [
                name
                for name, val in [
                    ("STRAVA_CLIENT_ID", self.strava_client_id),
                    ("STRAVA_CLIENT_SECRET", self.strava_client_secret),
                    ("STRAVA_REFRESH_TOKEN", self.strava_refresh_token),
                    ("STRAVA_VERIFY_TOKEN", self.strava_verify_token),
                    ("ATHLETE_ID", self.athlete_id),
                ]
                if not val
            ]
            if missing:
                raise ValueError(
                    f"MODE=webhook requires these env vars: {', '.join(missing)}"
                )

        if self.mode == "manual" and not self.access_password:
            raise ValueError("ACCESS_PASSWORD is required when MODE=manual")

        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()

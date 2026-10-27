"""
Central configuration loaded from environment variables / .env file.
Never log or print any of these values — they contain secrets.
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # ── Strava OAuth ──────────────────────────────────────────────────────────
    strava_client_id: str
    strava_client_secret: str
    # Bootstrap value only — the app persists the latest token in SQLite.
    # If the database is lost, re-run setup_oauth.py and update this value.
    strava_refresh_token: str
    strava_verify_token: str
    athlete_id: int

    # Strava API base URL.
    # UPDATE to https://api-v3.strava.com/api/v3 after January 4, 2027.
    strava_base_url: str = "https://www.strava.com/api/v3"

    # ── OpenAI ────────────────────────────────────────────────────────────────
    openai_api_key: str
    openai_model: str = "gpt-4o-mini"

    # ── App behaviour ─────────────────────────────────────────────────────────
    # When True, the generated title is logged but Strava is NOT updated.
    dry_run: bool = False

    # SQLite database file path. Override via DB_PATH env var in Docker.
    db_path: str = "run_titler.db"


@lru_cache
def get_settings() -> Settings:
    return Settings()

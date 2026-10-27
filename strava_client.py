"""
Strava API client with automatic token refresh and rate-limit awareness.
"""

import asyncio
import logging
import random
import time
from typing import Optional

import httpx

from config import get_settings
from database import get_tokens, save_tokens

logger = logging.getLogger(__name__)

# Strava auto-generated default titles that are safe to overwrite.
# The app will NOT rename any title that isn't in this set.
STRAVA_DEFAULT_TITLES: set[str] = {
    # Runs
    "Morning Run", "Afternoon Run", "Evening Run", "Night Run", "Lunch Run",
    # Walks
    "Morning Walk", "Afternoon Walk", "Evening Walk", "Night Walk", "Lunch Walk",
    # Workouts / generic
    "Morning Workout", "Afternoon Workout", "Evening Workout", "Night Workout",
    "Workout", "Activity", "Run", "Walk",
    "Morning Activity", "Afternoon Activity", "Evening Activity", "Night Activity",
}

MAX_RETRIES = 4
BASE_RETRY_DELAY = 30  # seconds between retries after the initial wait


class StravaClient:
    def __init__(self) -> None:
        self._settings = get_settings()
        self._access_token: Optional[str] = None
        self._refresh_token: Optional[str] = None
        self._expires_at: int = 0
        self._tokens_loaded: bool = False

    # ── Token management ──────────────────────────────────────────────────────

    async def _load_tokens(self) -> None:
        """Load tokens from SQLite; fall back to env var for bootstrap."""
        tokens = await get_tokens()
        if tokens:
            self._access_token = tokens["access_token"]
            self._refresh_token = tokens["refresh_token"]
            self._expires_at = tokens["expires_at"]
            logger.debug("Loaded tokens from database")
        else:
            # First run — bootstrap from the env var.
            self._refresh_token = self._settings.strava_refresh_token
            self._expires_at = 0  # force an immediate refresh
            logger.info("No tokens in database; will refresh using env var")
        self._tokens_loaded = True

    async def _ensure_valid_token(self) -> None:
        if not self._tokens_loaded:
            await self._load_tokens()
        # Refresh 60 s before actual expiry to avoid edge cases.
        if time.time() >= self._expires_at - 60:
            await self._refresh_access_token()

    async def _refresh_access_token(self) -> None:
        if not self._refresh_token:
            raise RuntimeError(
                "No Strava refresh token available. Run setup_oauth.py first."
            )
        logger.info("Refreshing Strava access token")
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(
                "https://www.strava.com/oauth/token",
                data={
                    "client_id": self._settings.strava_client_id,
                    "client_secret": self._settings.strava_client_secret,
                    "grant_type": "refresh_token",
                    "refresh_token": self._refresh_token,
                },
            )
            resp.raise_for_status()
            data = resp.json()

        self._access_token = data["access_token"]
        self._refresh_token = data["refresh_token"]
        self._expires_at = data["expires_at"]
        await save_tokens(self._access_token, self._refresh_token, self._expires_at)
        # Log expires_at, never the token values
        logger.info("Access token refreshed (expires_at=%d)", self._expires_at)

    async def _auth_headers(self) -> dict[str, str]:
        await self._ensure_valid_token()
        return {"Authorization": f"Bearer {self._access_token}"}

    # ── Rate-limit helpers ────────────────────────────────────────────────────

    @staticmethod
    def _log_rate_limit(response: httpx.Response) -> None:
        usage = response.headers.get("X-RateLimit-Usage", "")
        limit = response.headers.get("X-RateLimit-Limit", "")
        if usage and limit:
            try:
                short_u, daily_u = map(int, usage.split(","))
                short_l, daily_l = map(int, limit.split(","))
                if short_u >= short_l * 0.85 or daily_u >= daily_l * 0.85:
                    logger.warning(
                        "Strava rate limit approaching: 15-min %d/%d, daily %d/%d",
                        short_u, short_l, daily_u, daily_l,
                    )
            except (ValueError, AttributeError):
                pass

    # ── Public methods ────────────────────────────────────────────────────────

    async def fetch_activity(self, activity_id: int) -> Optional[dict]:
        """
        Fetch an activity from Strava.

        Waits 30–60 s before the first attempt (Strava needs time to
        process GPS and calculate stats), then retries up to MAX_RETRIES
        times with increasing delays.
        """
        initial_wait = random.randint(30, 60)
        logger.info(
            "Waiting %d s before fetching activity %d (Strava processing time)",
            initial_wait, activity_id,
        )
        await asyncio.sleep(initial_wait)

        for attempt in range(MAX_RETRIES):
            try:
                headers = await self._auth_headers()
                async with httpx.AsyncClient(timeout=30.0) as client:
                    resp = await client.get(
                        f"{self._settings.strava_base_url}/activities/{activity_id}",
                        headers=headers,
                    )

                if resp.status_code == 429:
                    wait = BASE_RETRY_DELAY * (2 ** attempt) + random.randint(5, 15)
                    logger.warning(
                        "Rate limited (429) fetching activity %d. "
                        "Backing off %d s (attempt %d/%d)",
                        activity_id, wait, attempt + 1, MAX_RETRIES,
                    )
                    await asyncio.sleep(wait)
                    continue

                self._log_rate_limit(resp)
                resp.raise_for_status()
                data: dict = resp.json()

                # Strava sometimes returns the activity before stats are ready.
                if not data.get("distance") or not data.get("moving_time"):
                    wait = BASE_RETRY_DELAY * (attempt + 1)
                    logger.info(
                        "Activity %d stats not ready yet, retrying in %d s",
                        activity_id, wait,
                    )
                    await asyncio.sleep(wait)
                    continue

                logger.info("Fetched activity %d successfully", activity_id)
                return data

            except httpx.HTTPStatusError as exc:
                logger.error(
                    "HTTP %d error fetching activity %d",
                    exc.response.status_code, activity_id,
                )
            except httpx.RequestError as exc:
                logger.error("Request error fetching activity %d: %s", activity_id, exc)

            if attempt < MAX_RETRIES - 1:
                delay = BASE_RETRY_DELAY * (attempt + 1)
                logger.info("Retrying in %d s (attempt %d/%d)", delay, attempt + 1, MAX_RETRIES)
                await asyncio.sleep(delay)

        logger.error(
            "Gave up fetching activity %d after %d attempts", activity_id, MAX_RETRIES
        )
        return None

    async def update_activity_name(self, activity_id: int, name: str) -> bool:
        """Rename an activity on Strava. Returns True on success."""
        try:
            headers = await self._auth_headers()
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.put(
                    f"{self._settings.strava_base_url}/activities/{activity_id}",
                    headers=headers,
                    json={"name": name},
                )

            if resp.status_code == 429:
                logger.warning(
                    "Rate limited (429) when updating activity %d", activity_id
                )
                return False

            self._log_rate_limit(resp)
            resp.raise_for_status()
            logger.info("Renamed activity %d to: %s", activity_id, name)
            return True

        except httpx.HTTPStatusError as exc:
            logger.error(
                "HTTP %d error updating activity %d",
                exc.response.status_code, activity_id,
            )
        except httpx.RequestError as exc:
            logger.error("Request error updating activity %d: %s", activity_id, exc)

        return False

    def is_default_title(self, title: str) -> bool:
        """
        Return True if the title is one of Strava's auto-generated defaults.
        Exact match first; then prefix match for variants like "Morning Run #2".
        """
        if title in STRAVA_DEFAULT_TITLES:
            return True
        for default in STRAVA_DEFAULT_TITLES:
            if title.startswith(default):
                return True
        return False

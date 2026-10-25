"""
run-titler — FastAPI server.

Serves two independent features:
  - Manual web UI  (GET /, POST /api/titles)  — works with MODE=manual or webhook
  - Strava webhook (GET /webhook, POST /webhook) — only active when MODE=webhook
"""

import logging
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from pathlib import Path
from threading import Lock
from typing import Literal, Optional

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from config import get_settings
from database import (
    cleanup_old_data,
    get_recent_activity_stats,
    init_db,
    is_activity_processed,
    mark_activity_processed,
)
from openai_client import generate_title, generate_titles_manual
from strava_client import StravaClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

_strava: Optional[StravaClient] = None

# Load the UI template once at startup
_INDEX_HTML = (Path(__file__).parent / "templates" / "index.html").read_text(
    encoding="utf-8"
)


# ── Rate limiter ──────────────────────────────────────────────────────────────

class _RateLimiter:
    """Simple in-memory sliding-window rate limiter, keyed by client IP."""

    def __init__(self, max_requests: int = 20, window_seconds: int = 60) -> None:
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._log: dict[str, list[float]] = defaultdict(list)
        self._lock = Lock()

    def is_allowed(self, key: str) -> bool:
        now = time.time()
        cutoff = now - self.window_seconds
        with self._lock:
            self._log[key] = [t for t in self._log[key] if t > cutoff]
            if len(self._log[key]) >= self.max_requests:
                return False
            self._log[key].append(now)
            return True


_rate_limiter = _RateLimiter(max_requests=20, window_seconds=60)


def _client_ip(request: Request) -> str:
    """Return the real client IP, respecting X-Forwarded-For from proxies."""
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


# ── App lifecycle ─────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):  # noqa: ARG001
    global _strava
    settings = get_settings()
    logger.info("run-titler starting  mode=%s  dry_run=%s", settings.mode, settings.dry_run)

    if settings.mode == "webhook":
        await init_db()
        _strava = StravaClient()

    yield
    logger.info("run-titler shutting down")


app = FastAPI(title="run-titler", lifespan=lifespan)


# ── Manual UI — Password models ───────────────────────────────────────────────

class _PasswordCheck(BaseModel):
    password: str


class _TitleRequest(BaseModel):
    password: str
    distance_km: float = Field(gt=0, le=300, description="Distance in kilometres")
    time_minutes: int = Field(gt=0, le=720, description="Moving time in minutes")
    run_type: Literal["Easy", "Tempo", "Long", "Intervals", "Recovery", "Race"]
    time_of_day: Literal["Morning", "Afternoon", "Evening", "Night"]
    tone: Literal["Fun", "Motivational", "Minimal", "Funny"]
    notes: str = Field(default="", max_length=200)


class _TitleResponse(BaseModel):
    titles: list[str]


# ── Manual UI — Routes ────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def index() -> HTMLResponse:
    return HTMLResponse(_INDEX_HTML)


@app.post("/api/check-password")
async def check_password(body: _PasswordCheck) -> dict:
    """
    Validate the access password without spending an OpenAI call.
    Returns 200 if correct, 401 if wrong, 204 if no password is configured.
    """
    settings = get_settings()
    if not settings.access_password:
        return {"ok": True}  # no password set — always allow
    if body.password != settings.access_password:
        raise HTTPException(status_code=401, detail="Invalid password")
    return {"ok": True}


@app.post("/api/titles", response_model=_TitleResponse)
async def api_titles(body: _TitleRequest, request: Request) -> _TitleResponse:
    """Generate 3 title suggestions for a manually-entered run."""
    settings = get_settings()

    # ── Auth ──────────────────────────────────────────────────────────────────
    if settings.access_password and body.password != settings.access_password:
        raise HTTPException(status_code=401, detail="Invalid password")

    # ── Rate limit ────────────────────────────────────────────────────────────
    ip = _client_ip(request)
    if not _rate_limiter.is_allowed(ip):
        logger.warning("Rate limit hit for IP %s", ip)
        raise HTTPException(
            status_code=429, detail="Too many requests — wait a moment and try again"
        )

    logger.info(
        "Manual title request — distance=%.1fkm time=%dmin type=%s tone=%s",
        body.distance_km, body.time_minutes, body.run_type, body.tone,
    )

    titles = await generate_titles_manual(
        distance_km=body.distance_km,
        time_minutes=body.time_minutes,
        run_type=body.run_type,
        time_of_day=body.time_of_day,
        tone=body.tone,
        notes=body.notes,
    )
    return _TitleResponse(titles=titles)


# ── Webhook — Strava integration ──────────────────────────────────────────────

@app.get("/webhook")
async def webhook_verify(
    hub_mode: str = Query(alias="hub.mode"),
    hub_verify_token: str = Query(alias="hub.verify_token"),
    hub_challenge: str = Query(alias="hub.challenge"),
) -> dict:
    """Strava subscription validation — only active when MODE=webhook."""
    settings = get_settings()
    if settings.mode != "webhook":
        raise HTTPException(status_code=404)

    if hub_mode != "subscribe" or hub_verify_token != settings.strava_verify_token:
        logger.warning("Webhook verification failed")
        raise HTTPException(status_code=403, detail="Forbidden")

    logger.info("Webhook subscription verified")
    return {"hub.challenge": hub_challenge}


@app.post("/webhook")
async def webhook_event(
    request: Request, background_tasks: BackgroundTasks
) -> JSONResponse:
    """Receive Strava activity events — only processed when MODE=webhook."""
    settings = get_settings()
    if settings.mode != "webhook":
        raise HTTPException(status_code=404)

    try:
        event: dict = await request.json()
    except Exception:
        return JSONResponse(status_code=200, content={"status": "ok"})

    object_type = event.get("object_type")
    aspect_type = event.get("aspect_type")
    owner_id = event.get("owner_id")
    activity_id = event.get("object_id")

    logger.info(
        "Webhook: object_type=%s aspect_type=%s owner_id=%s activity_id=%s",
        object_type, aspect_type, owner_id, activity_id,
    )

    # Only act on new activity creates from our athlete.
    # Ignoring "update" prevents an infinite rename loop.
    if (
        object_type == "activity"
        and aspect_type == "create"
        and owner_id == settings.athlete_id
        and activity_id is not None
    ):
        background_tasks.add_task(process_activity, int(activity_id))

    return JSONResponse(status_code=200, content={"status": "ok"})


async def process_activity(activity_id: int) -> None:
    logger.info("--- Processing activity %d ---", activity_id)
    settings = get_settings()

    try:
        if await is_activity_processed(activity_id):
            logger.info("Activity %d already processed", activity_id)
            return

        activity = await _strava.fetch_activity(activity_id)
        if not activity:
            logger.error("Could not fetch activity %d", activity_id)
            return

        current_title: str = activity.get("name", "")
        sport_type: str = activity.get("sport_type") or activity.get("type") or ""
        distance_km = activity.get("distance", 0) / 1000
        moving_time = activity.get("moving_time", 0)

        if "run" not in sport_type.lower():
            logger.info("Activity %d is %s — skipping", activity_id, sport_type)
            await mark_activity_processed(activity_id, distance_km, moving_time, current_title)
            return

        if not _strava.is_default_title(current_title):
            logger.info("Activity %d has custom title %r — leaving unchanged", activity_id, current_title)
            await mark_activity_processed(activity_id, distance_km, moving_time, current_title)
            return

        recent_stats = await get_recent_activity_stats()
        new_title = await generate_title(activity, recent_stats)
        logger.info("Activity %d  %r → %r  dry_run=%s", activity_id, current_title, new_title, settings.dry_run)

        if settings.dry_run:
            logger.info("[DRY RUN] Would rename %d to: %s", activity_id, new_title)
        else:
            success = await _strava.update_activity_name(activity_id, new_title)
            if not success:
                logger.error("Failed to rename activity %d on Strava", activity_id)

        await mark_activity_processed(activity_id, distance_km, moving_time, new_title)
        await cleanup_old_data()

    except Exception:  # noqa: BLE001
        logger.exception("Unexpected error processing activity %d", activity_id)


# ── Health ────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}

"""
run-titler — FastAPI webhook server.

Listens for Strava webhook events, fetches new run activities,
generates a title with OpenAI, and writes it back to Strava.
"""

import logging
from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from config import get_settings
from database import (
    cleanup_old_data,
    get_recent_activity_stats,
    init_db,
    is_activity_processed,
    mark_activity_processed,
)
from openai_client import generate_title
from strava_client import StravaClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# Single shared client — holds the in-memory token cache.
_strava = StravaClient()


@asynccontextmanager
async def lifespan(app: FastAPI):  # noqa: ARG001
    await init_db()
    settings = get_settings()
    logger.info(
        "run-titler started  dry_run=%s  athlete_id=%s",
        settings.dry_run,
        settings.athlete_id,
    )
    yield
    logger.info("run-titler shutting down")


app = FastAPI(title="run-titler", lifespan=lifespan)


# ── Webhook validation (GET) ──────────────────────────────────────────────────

@app.get("/webhook")
async def webhook_verify(
    hub_mode: str = Query(alias="hub.mode"),
    hub_verify_token: str = Query(alias="hub.verify_token"),
    hub_challenge: str = Query(alias="hub.challenge"),
) -> dict:
    """
    Strava sends this GET request once when you register a webhook subscription.
    Respond with the challenge to confirm ownership of the callback URL.
    """
    settings = get_settings()
    if hub_mode != "subscribe" or hub_verify_token != settings.strava_verify_token:
        logger.warning(
            "Webhook verification failed: mode=%s token_match=%s",
            hub_mode,
            hub_verify_token == settings.strava_verify_token,
        )
        raise HTTPException(status_code=403, detail="Forbidden")

    logger.info("Webhook subscription verified successfully")
    return {"hub.challenge": hub_challenge}


# ── Webhook event receiver (POST) ─────────────────────────────────────────────

@app.post("/webhook")
async def webhook_event(request: Request, background_tasks: BackgroundTasks) -> JSONResponse:
    """
    Strava POSTs every activity event here. We must reply within ~2 seconds,
    so all real work is done in a background task.
    """
    try:
        event: dict = await request.json()
    except Exception:
        # Malformed body — still return 200 so Strava doesn't retry.
        return JSONResponse(status_code=200, content={"status": "ok"})

    object_type = event.get("object_type")
    aspect_type = event.get("aspect_type")
    owner_id = event.get("owner_id")
    activity_id = event.get("object_id")

    logger.info(
        "Webhook: object_type=%s aspect_type=%s owner_id=%s activity_id=%s",
        object_type, aspect_type, owner_id, activity_id,
    )

    settings = get_settings()

    # Only process new activity creates from our athlete.
    # Ignoring "update" prevents an infinite loop — our own rename triggers
    # an update event, which we must not act on.
    if (
        object_type == "activity"
        and aspect_type == "create"
        and owner_id == settings.athlete_id
        and activity_id is not None
    ):
        background_tasks.add_task(process_activity, int(activity_id))
    else:
        logger.debug(
            "Skipping event (not a new activity or wrong owner): "
            "object_type=%s aspect_type=%s owner_id=%s",
            object_type, aspect_type, owner_id,
        )

    return JSONResponse(status_code=200, content={"status": "ok"})


# ── Background processing ─────────────────────────────────────────────────────

async def process_activity(activity_id: int) -> None:
    """
    Full pipeline for a single activity:
      1. Dedup check
      2. Fetch from Strava (with initial wait + retries)
      3. Check sport type and current title
      4. Generate title with OpenAI
      5. Update Strava (or log if DRY_RUN)
      6. Mark as processed
    """
    logger.info("--- Processing activity %d ---", activity_id)
    settings = get_settings()

    try:
        # ── 1. Dedup ──────────────────────────────────────────────────────────
        if await is_activity_processed(activity_id):
            logger.info("Activity %d already processed, skipping", activity_id)
            return

        # ── 2. Fetch ──────────────────────────────────────────────────────────
        activity = await _strava.fetch_activity(activity_id)
        if not activity:
            logger.error("Could not fetch activity %d; giving up", activity_id)
            return

        current_title: str = activity.get("name", "")
        sport_type: str = activity.get("sport_type") or activity.get("type") or ""
        distance_km = activity.get("distance", 0) / 1000
        moving_time = activity.get("moving_time", 0)

        # ── 3a. Sport type filter ─────────────────────────────────────────────
        if "run" not in sport_type.lower():
            logger.info(
                "Activity %d is not a run (sport_type=%s); skipping title generation",
                activity_id, sport_type,
            )
            await mark_activity_processed(activity_id, distance_km, moving_time, current_title)
            return

        # ── 3b. Safety: never overwrite a custom title ────────────────────────
        if not _strava.is_default_title(current_title):
            logger.info(
                "Activity %d has a custom title (%r); leaving it unchanged",
                activity_id, current_title,
            )
            await mark_activity_processed(activity_id, distance_km, moving_time, current_title)
            return

        # ── 4. Generate title ─────────────────────────────────────────────────
        recent_stats = await get_recent_activity_stats()
        new_title = await generate_title(activity, recent_stats)

        logger.info(
            "Activity %d  old=%r  new=%r  dry_run=%s",
            activity_id, current_title, new_title, settings.dry_run,
        )

        # ── 5. Update Strava (or dry-run) ─────────────────────────────────────
        if settings.dry_run:
            logger.info(
                "[DRY RUN] Would rename activity %d to: %s", activity_id, new_title
            )
        else:
            success = await _strava.update_activity_name(activity_id, new_title)
            if not success:
                logger.error(
                    "Failed to update activity %d on Strava; "
                    "marking as processed to avoid repeated attempts",
                    activity_id,
                )

        # ── 6. Mark processed (even on dry-run, to avoid re-processing) ───────
        await mark_activity_processed(activity_id, distance_km, moving_time, new_title)

        # Periodic data retention cleanup — runs after every processed activity.
        await cleanup_old_data()

    except Exception:  # noqa: BLE001
        logger.exception("Unexpected error processing activity %d", activity_id)


# ── Health check ──────────────────────────────────────────────────────────────

@app.get("/health")
async def health() -> dict:
    """Used by Render/Railway/Docker healthchecks."""
    return {"status": "ok"}

"""
OpenAI title generation.

Sends only a compact, privacy-respecting summary of the run — no GPS data,
no heart rate streams, no personal name. The model returns a single short title.
"""

import logging
from datetime import datetime
from typing import Optional

from openai import AsyncOpenAI

from config import get_settings

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You are a running activity title generator. Your job is to create one short, \
fun, accurate title for a run.

Rules:
- Maximum 6 words
- No quotation marks
- No emojis
- No hashtags
- Reflect the character of the run (easy, tempo, long, recovery, intervals, etc.)
- Motivational but not cheesy
- Short and punchy — like a headline, not a sentence

Good examples:
  Easy Sunday Morning Miles
  Solid Tempo Push
  Long Run Done Right
  Intervals in the Rain
  Recovery Shuffle
  Hilly Tuesday Grind
  Crisp Lunchtime Easy Jog
"""


# ── Helpers ───────────────────────────────────────────────────────────────────

def _format_pace(seconds_per_km: float) -> str:
    minutes = int(seconds_per_km // 60)
    secs = int(seconds_per_km % 60)
    return f"{minutes}:{secs:02d}/km"


def _format_duration(seconds: int) -> str:
    hours = seconds // 3600
    mins = (seconds % 3600) // 60
    return f"{hours}h {mins}m" if hours else f"{mins}m"


def _classify_run(distance_km: float, pace_sec_per_km: float, elevation_m: float) -> str:
    """Rough classification based on pace and distance."""
    if distance_km >= 18:
        return "long run"
    if pace_sec_per_km > 0 and pace_sec_per_km < 270:   # faster than 4:30/km
        return "tempo or interval run"
    if pace_sec_per_km > 0 and pace_sec_per_km < 330:   # 4:30–5:30/km
        return "moderate run"
    if elevation_m >= 300:
        return "hilly run"
    if pace_sec_per_km > 420:                            # slower than 7:00/km
        return "recovery run"
    return "easy run"


def _time_of_day(hour: int) -> str:
    if 5 <= hour < 12:
        return "morning"
    if 12 <= hour < 17:
        return "afternoon"
    if 17 <= hour < 21:
        return "evening"
    return "night"


def _build_summary(activity: dict, recent_stats: list[dict]) -> str:
    distance_m: float = activity.get("distance", 0)
    distance_km = distance_m / 1000
    moving_time: int = activity.get("moving_time", 0)
    elevation_gain: float = activity.get("total_elevation_gain", 0)
    sport_type: str = activity.get("sport_type") or activity.get("type") or "Run"

    pace_sec_per_km = (moving_time / distance_km) if distance_km > 0 else 0
    pace_str = _format_pace(pace_sec_per_km) if pace_sec_per_km > 0 else "unknown"
    duration_str = _format_duration(moving_time)

    # Parse local start time — Strava appends "Z" to start_date_local even
    # though it's already in the athlete's local timezone, so we strip it.
    start_local: str = activity.get("start_date_local", "")
    try:
        dt = datetime.fromisoformat(start_local.rstrip("Z"))
        day_of_week = dt.strftime("%A")
        tod = _time_of_day(dt.hour)
    except (ValueError, AttributeError):
        day_of_week = "unknown day"
        tod = "unknown time"

    run_char = _classify_run(distance_km, pace_sec_per_km, elevation_gain)

    # Compare to recent runs
    comparisons: list[str] = []
    if recent_stats:
        avg_dist = sum(r["distance_km"] for r in recent_stats) / len(recent_stats)
        avg_time = sum(r["moving_time_seconds"] for r in recent_stats) / len(recent_stats)
        avg_pace = avg_time / avg_dist if avg_dist > 0 else 0

        if distance_km > avg_dist * 1.2:
            comparisons.append("longer than usual")
        elif distance_km < avg_dist * 0.8:
            comparisons.append("shorter than usual")

        if pace_sec_per_km > 0 and avg_pace > 0:
            if pace_sec_per_km < avg_pace * 0.95:
                comparisons.append("faster than usual")
            elif pace_sec_per_km > avg_pace * 1.05:
                comparisons.append("slower than usual")

    comparison_str = ", ".join(comparisons) if comparisons else "typical effort"

    return (
        f"Sport: {sport_type}\n"
        f"Distance: {distance_km:.2f} km\n"
        f"Duration: {duration_str}\n"
        f"Average pace: {pace_str}\n"
        f"Elevation gain: {elevation_gain:.0f} m\n"
        f"Time of day: {tod}\n"
        f"Day of week: {day_of_week}\n"
        f"Run character: {run_char}\n"
        f"Compared to recent runs: {comparison_str}"
    )


def _fallback_title(activity: dict) -> str:
    """Template title used when OpenAI fails or returns invalid output."""
    distance_km = activity.get("distance", 0) / 1000
    moving_time = activity.get("moving_time", 0)
    elevation_gain = activity.get("total_elevation_gain", 0)
    pace_sec_per_km = (moving_time / distance_km) if distance_km > 0 else 0

    run_char = _classify_run(distance_km, pace_sec_per_km, elevation_gain)
    # Capitalise each word (e.g. "easy run" → "Easy Run")
    run_char_title = run_char.title()

    # Format distance: "5.0km" → "5K", "10.4km" → "10.4km"
    if distance_km >= 10:
        dist_str = f"{distance_km:.0f}km"
    elif distance_km >= 1:
        dist_str = f"{distance_km:.1f}km"
    else:
        dist_str = f"{int(distance_km * 1000)}m"

    start_local = activity.get("start_date_local", "")
    try:
        dt = datetime.fromisoformat(start_local.rstrip("Z"))
        day = dt.strftime("%A")
        tod = _time_of_day(dt.hour).capitalize()
    except (ValueError, AttributeError):
        day = ""
        tod = ""

    parts = [p for p in [run_char_title, dist_str, day, tod] if p]
    return " ".join(parts)


# ── Public API ────────────────────────────────────────────────────────────────

async def generate_title(activity: dict, recent_stats: list[dict]) -> str:
    """
    Ask OpenAI for a short title and validate the response.
    Returns a fallback template title if anything goes wrong.
    """
    settings = get_settings()
    activity_id: Optional[int] = activity.get("id")
    summary = _build_summary(activity, recent_stats)
    logger.info("Generating title for activity %s", activity_id)
    logger.debug("Run summary:\n%s", summary)

    client = AsyncOpenAI(api_key=settings.openai_api_key)

    try:
        response = await client.chat.completions.create(
            model=settings.openai_model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": f"Generate a title for this run:\n\n{summary}",
                },
            ],
            max_tokens=30,
            temperature=0.8,
        )
        raw = response.choices[0].message.content or ""
        title = raw.strip().strip("\"'")

        if not title:
            logger.warning("OpenAI returned an empty title; using fallback")
            return _fallback_title(activity)

        if len(title) > 60:
            logger.warning(
                "OpenAI title too long (%d chars), using fallback. Got: %s",
                len(title), title,
            )
            return _fallback_title(activity)

        logger.info("Generated title: %s", title)
        return title

    except Exception as exc:  # noqa: BLE001
        logger.error("OpenAI error generating title for activity %s: %s", activity_id, exc)
        fallback = _fallback_title(activity)
        logger.info("Using fallback title: %s", fallback)
        return fallback

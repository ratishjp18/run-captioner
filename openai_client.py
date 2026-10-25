"""
OpenAI title generation — used by both the webhook pipeline and the manual web UI.

Webhook path:  generate_title(activity, recent_stats)  → single string
Manual path:   generate_titles_manual(...)             → list of 3 strings
"""

import json
import logging
from datetime import datetime
from typing import Optional

from openai import AsyncOpenAI

from config import get_settings

logger = logging.getLogger(__name__)


# ── Webhook system prompt (unchanged) ────────────────────────────────────────

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

# ── Manual mode system prompt ─────────────────────────────────────────────────

MANUAL_SYSTEM_PROMPT = """\
You are a running activity title generator. Generate exactly 3 short, distinct \
titles for a run.

Return ONLY a JSON object in this exact format:
{"titles": ["Title One", "Title Two", "Title Three"]}

Rules for each title:
- Maximum 6 words
- No quotation marks inside the title text
- No emojis
- No hashtags
- Do not invent facts not given (no weather, specific place names, or details \
unless they appear in the notes)
- Each title should feel meaningfully different from the others

Tone guide (apply the requested tone to all 3 titles):
- Fun: creative and playful, grounded in the run facts
- Motivational: inspiring and energizing, celebrates the effort
- Minimal: stripped back and clean, just the essential facts
- Funny: light-hearted and gently humorous, not silly
"""


# ── Shared helpers ────────────────────────────────────────────────────────────

def _format_pace(seconds_per_km: float) -> str:
    minutes = int(seconds_per_km // 60)
    secs = int(seconds_per_km % 60)
    return f"{minutes}:{secs:02d}/km"


def _format_duration(seconds: int) -> str:
    hours = seconds // 3600
    mins = (seconds % 3600) // 60
    return f"{hours}h {mins}m" if hours else f"{mins}m"


def _classify_run(distance_km: float, pace_sec_per_km: float, elevation_m: float) -> str:
    if distance_km >= 18:
        return "long run"
    if pace_sec_per_km > 0 and pace_sec_per_km < 270:
        return "tempo or interval run"
    if pace_sec_per_km > 0 and pace_sec_per_km < 330:
        return "moderate run"
    if elevation_m >= 300:
        return "hilly run"
    if pace_sec_per_km > 420:
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


# ── Webhook pipeline ──────────────────────────────────────────────────────────

def _build_webhook_summary(activity: dict, recent_stats: list[dict]) -> str:
    distance_km = activity.get("distance", 0) / 1000
    moving_time: int = activity.get("moving_time", 0)
    elevation_gain: float = activity.get("total_elevation_gain", 0)
    sport_type: str = activity.get("sport_type") or activity.get("type") or "Run"

    pace_sec_per_km = (moving_time / distance_km) if distance_km > 0 else 0
    pace_str = _format_pace(pace_sec_per_km) if pace_sec_per_km > 0 else "unknown"
    duration_str = _format_duration(moving_time)

    start_local: str = activity.get("start_date_local", "")
    try:
        dt = datetime.fromisoformat(start_local.rstrip("Z"))
        day_of_week = dt.strftime("%A")
        tod = _time_of_day(dt.hour)
    except (ValueError, AttributeError):
        day_of_week = "unknown day"
        tod = "unknown time"

    run_char = _classify_run(distance_km, pace_sec_per_km, elevation_gain)

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


def _fallback_webhook_title(activity: dict) -> str:
    distance_km = activity.get("distance", 0) / 1000
    moving_time = activity.get("moving_time", 0)
    elevation_gain = activity.get("total_elevation_gain", 0)
    pace_sec_per_km = (moving_time / distance_km) if distance_km > 0 else 0
    run_char = _classify_run(distance_km, pace_sec_per_km, elevation_gain).title()

    dist_str = (
        f"{distance_km:.0f}km" if distance_km >= 10 else f"{distance_km:.1f}km"
    )

    start_local = activity.get("start_date_local", "")
    try:
        dt = datetime.fromisoformat(start_local.rstrip("Z"))
        day = dt.strftime("%A")
        tod = _time_of_day(dt.hour).capitalize()
    except (ValueError, AttributeError):
        day = ""
        tod = ""

    parts = [p for p in [run_char, dist_str, day, tod] if p]
    return " ".join(parts)


async def generate_title(activity: dict, recent_stats: list[dict]) -> str:
    """Webhook pipeline: return one title for a Strava activity dict."""
    settings = get_settings()
    activity_id: Optional[int] = activity.get("id")
    summary = _build_webhook_summary(activity, recent_stats)
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
            logger.warning("OpenAI returned empty title; using fallback")
            return _fallback_webhook_title(activity)
        if len(title) > 60:
            logger.warning("OpenAI title too long (%d chars); using fallback", len(title))
            return _fallback_webhook_title(activity)
        logger.info("Generated title: %s", title)
        return title
    except Exception as exc:  # noqa: BLE001
        logger.error("OpenAI error for activity %s: %s", activity_id, exc)
        fallback = _fallback_webhook_title(activity)
        logger.info("Using fallback: %s", fallback)
        return fallback


# ── Manual web UI ─────────────────────────────────────────────────────────────

def _fallback_titles_manual(
    distance_km: float,
    time_minutes: int,
    run_type: str,
    time_of_day: str,
) -> list[str]:
    dist_str = (
        f"{distance_km:.0f}km" if distance_km >= 10 else f"{distance_km:.1f}km"
    )
    tod = time_of_day.capitalize()
    rt = run_type.capitalize()
    pace_sec = (time_minutes * 60) / distance_km if distance_km > 0 else 0
    pace_str = _format_pace(pace_sec) if pace_sec > 0 else ""

    return [
        f"{tod} {rt} Run",
        f"{rt} {dist_str} Effort",
        f"{dist_str} at {pace_str}" if pace_str else f"Solid {rt} Effort",
    ]


async def generate_titles_manual(
    distance_km: float,
    time_minutes: int,
    run_type: str,
    time_of_day: str,
    tone: str,
    notes: str = "",
) -> list[str]:
    """
    Manual UI path: return 3 title suggestions based on form input.
    Pace is calculated here — never delegated to the AI.
    """
    settings = get_settings()

    # Calculate pace in-app, as specified
    pace_sec_per_km = (time_minutes * 60) / distance_km if distance_km > 0 else 0
    pace_str = _format_pace(pace_sec_per_km) if pace_sec_per_km > 0 else "unknown"

    summary_parts = [
        f"Distance: {distance_km:.1f} km",
        f"Duration: {time_minutes} min",
        f"Average pace: {pace_str}",
        f"Run type: {run_type}",
        f"Time of day: {time_of_day}",
        f"Tone: {tone}",
    ]
    if notes:
        summary_parts.append(f"Notes: {notes}")

    summary = "\n".join(summary_parts)
    logger.info(
        "Generating manual titles — run_type=%s distance=%.1fkm tone=%s",
        run_type, distance_km, tone,
    )

    client = AsyncOpenAI(api_key=settings.openai_api_key)
    try:
        response = await client.chat.completions.create(
            model=settings.openai_model,
            messages=[
                {"role": "system", "content": MANUAL_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": f"Generate 3 titles for this run:\n\n{summary}",
                },
            ],
            response_format={"type": "json_object"},
            max_tokens=120,
            temperature=0.9,
        )
        raw = response.choices[0].message.content or "{}"
        data = json.loads(raw)
        raw_titles: list = data.get("titles", [])

        valid: list[str] = []
        for t in raw_titles:
            t = str(t).strip().strip("\"'")
            if t and len(t) <= 60:
                valid.append(t)

        if valid:
            # Pad with fallbacks if OpenAI returned fewer than 3
            fallbacks = _fallback_titles_manual(distance_km, time_minutes, run_type, time_of_day)
            while len(valid) < 3:
                valid.append(fallbacks[len(valid)])
            logger.info("Manual titles: %s", valid[:3])
            return valid[:3]

        logger.warning("OpenAI returned no valid titles; using fallbacks")
        return _fallback_titles_manual(distance_km, time_minutes, run_type, time_of_day)

    except Exception as exc:  # noqa: BLE001
        logger.error("OpenAI error (manual): %s", exc)
        return _fallback_titles_manual(distance_km, time_minutes, run_type, time_of_day)

"""
SQLite persistence layer.

Tables:
  strava_tokens        — latest access/refresh token pair (single row)
  processed_activities — dedup log + basic stats for comparison
"""

import logging
import os

import aiosqlite

logger = logging.getLogger(__name__)

# Allow the path to be overridden by an environment variable so Docker
# can store the file on a mounted volume.
DB_PATH = os.getenv("DB_PATH", "run_titler.db")


async def init_db() -> None:
    """Create tables if they don't exist."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS strava_tokens (
                id            INTEGER PRIMARY KEY,
                access_token  TEXT    NOT NULL,
                refresh_token TEXT    NOT NULL,
                expires_at    INTEGER NOT NULL,
                updated_at    TEXT    DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS processed_activities (
                activity_id          INTEGER PRIMARY KEY,
                processed_at         TEXT    DEFAULT CURRENT_TIMESTAMP,
                distance_km          REAL,
                moving_time_seconds  INTEGER,
                title_assigned       TEXT
            )
        """)
        await db.commit()
    logger.info("Database ready at %s", DB_PATH)


# ── Token storage ─────────────────────────────────────────────────────────────

async def get_tokens() -> dict | None:
    """Return the stored token row, or None if the table is empty."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT access_token, refresh_token, expires_at "
            "FROM strava_tokens ORDER BY id DESC LIMIT 1"
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                return {
                    "access_token": row[0],
                    "refresh_token": row[1],
                    "expires_at": row[2],
                }
    return None


async def save_tokens(access_token: str, refresh_token: str, expires_at: int) -> None:
    """Overwrite the single token row — never log the actual token values."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM strava_tokens")
        await db.execute(
            "INSERT INTO strava_tokens (access_token, refresh_token, expires_at) "
            "VALUES (?, ?, ?)",
            (access_token, refresh_token, expires_at),
        )
        await db.commit()
    logger.info("Strava tokens saved to database (expires_at=%d)", expires_at)


# ── Activity dedup ────────────────────────────────────────────────────────────

async def is_activity_processed(activity_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT 1 FROM processed_activities WHERE activity_id = ?",
            (activity_id,),
        ) as cursor:
            return await cursor.fetchone() is not None


async def mark_activity_processed(
    activity_id: int,
    distance_km: float,
    moving_time_seconds: int,
    title: str,
) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT OR IGNORE INTO processed_activities
               (activity_id, distance_km, moving_time_seconds, title_assigned)
               VALUES (?, ?, ?, ?)""",
            (activity_id, distance_km, moving_time_seconds, title),
        )
        await db.commit()


# ── Recent stats for comparison ───────────────────────────────────────────────

async def get_recent_activity_stats(limit: int = 10) -> list[dict]:
    """
    Return basic stats for the most recent processed activities (up to 30 days).
    Used to determine whether today's run was longer or faster than usual.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """SELECT distance_km, moving_time_seconds
               FROM processed_activities
               WHERE processed_at >= datetime('now', '-30 days')
                 AND distance_km IS NOT NULL
                 AND moving_time_seconds IS NOT NULL
               ORDER BY processed_at DESC
               LIMIT ?""",
            (limit,),
        ) as cursor:
            rows = await cursor.fetchall()
            return [
                {"distance_km": row[0], "moving_time_seconds": row[1]}
                for row in rows
            ]


# ── Data retention ────────────────────────────────────────────────────────────

async def cleanup_old_data() -> None:
    """Remove processed activity records older than 7 days."""
    async with aiosqlite.connect(DB_PATH) as db:
        result = await db.execute(
            "DELETE FROM processed_activities "
            "WHERE processed_at < datetime('now', '-7 days')"
        )
        await db.commit()
        if result.rowcount:
            logger.info("Cleaned up %d old activity records", result.rowcount)

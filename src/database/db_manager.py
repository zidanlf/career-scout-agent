"""
Database Manager for Career Scout Agent
Handles all SQLite operations with async support via aiosqlite.
"""

import aiosqlite
import logging
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional

# Configure module logger
logger = logging.getLogger(__name__)

# Database path
DB_PATH = Path(__file__).parent.parent.parent / "data" / "scout.db"


async def init_db() -> None:
    """Initialize database with required tables."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    
    async with aiosqlite.connect(DB_PATH) as db:
        # Users table
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                telegram_id INTEGER PRIMARY KEY,
                name TEXT,
                rss_url TEXT,
                keywords TEXT,
                active INTEGER DEFAULT 1
            )
        """)
        
        # Migration: add keywords column if missing (for existing DBs)
        try:
            await db.execute("ALTER TABLE users ADD COLUMN keywords TEXT")
            await db.commit()
            logger.info("Migrated: added 'keywords' column to users table")
        except Exception:
            pass  # Column already exists
        
        # Migration: add active column if missing (for existing DBs)
        try:
            await db.execute("ALTER TABLE users ADD COLUMN active INTEGER DEFAULT 1")
            await db.commit()
            logger.info("Migrated: added 'active' column to users table")
        except Exception:
            pass  # Column already exists
        
        # Processed jobs table for deduplication
        await db.execute("""
            CREATE TABLE IF NOT EXISTS processed_jobs (
                job_hash TEXT,
                user_id INTEGER REFERENCES users(telegram_id) ON DELETE CASCADE,
                found_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(job_hash, user_id)
            )
        """)
        
        # Monitored URLs table for web discovery
        await db.execute("""
            CREATE TABLE IF NOT EXISTS monitored_urls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER REFERENCES users(telegram_id) ON DELETE CASCADE,
                url TEXT NOT NULL,
                label TEXT NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        await db.commit()
        logger.info("Database initialized successfully")


# ============== USER OPERATIONS ==============

async def upsert_user(telegram_id: int, name: str) -> None:
    """Insert or update a user in the database."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO users (telegram_id, name)
            VALUES (?, ?)
            ON CONFLICT(telegram_id) DO UPDATE SET name = excluded.name
            """,
            (telegram_id, name)
        )
        await db.commit()
        logger.info(f"User upserted: {telegram_id} ({name})")


async def get_user_rss(telegram_id: int) -> Optional[str]:
    """Get RSS URL for a user."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT rss_url FROM users WHERE telegram_id = ?",
            (telegram_id,)
        ) as cursor:
            row = await cursor.fetchone()
        return row[0] if row else None


async def get_all_users() -> list[dict]:
    """Get all registered users."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT telegram_id, name, rss_url, keywords, active FROM users"
        ) as cursor:
            rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def get_user_keywords(telegram_id: int) -> list[str]:
    """Get keyword list for a user. Returns empty list if none set."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT keywords FROM users WHERE telegram_id = ?",
            (telegram_id,)
        ) as cursor:
            row = await cursor.fetchone()
    if not row or not row[0]:
        return []
    return [k.strip().lower() for k in row[0].split(",") if k.strip()]


async def update_keywords(telegram_id: int, keywords_str: str) -> None:
    """Update keywords for a user. Pass empty string to clear."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET keywords = ? WHERE telegram_id = ?",
            (keywords_str if keywords_str else None, telegram_id)
        )
        await db.commit()
        logger.info(f"Keywords updated for user {telegram_id}: {keywords_str}")


async def set_user_active(telegram_id: int, active: bool) -> None:
    """Set user active status. Active=True means scanning is enabled."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET active = ? WHERE telegram_id = ?",
            (1 if active else 0, telegram_id)
        )
        await db.commit()
        status = "active" if active else "paused"
        logger.info(f"User {telegram_id} set to {status}")


async def get_user_active(telegram_id: int) -> bool:
    """Check if user scanning is active."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT active FROM users WHERE telegram_id = ?",
            (telegram_id,)
        ) as cursor:
            row = await cursor.fetchone()
        return bool(row[0]) if row else True


async def delete_keywords(telegram_id: int) -> None:
    """Remove all keywords for a user."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET keywords = NULL WHERE telegram_id = ?",
            (telegram_id,)
        )
        await db.commit()
        logger.info(f"Keywords deleted for user {telegram_id}")


# ============== RSS OPERATIONS ==============

async def update_rss(telegram_id: int, url: str) -> None:
    """Update RSS URL for a user."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET rss_url = ? WHERE telegram_id = ?",
            (url, telegram_id)
        )
        await db.commit()
        logger.info(f"RSS URL updated for user {telegram_id}")


async def delete_rss(telegram_id: int) -> None:
    """Remove RSS URL from user (set to NULL)."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET rss_url = NULL WHERE telegram_id = ?",
            (telegram_id,)
        )
        await db.commit()
        logger.info(f"RSS URL deleted for user {telegram_id}")


# ============== JOB OPERATIONS ==============

async def check_job_processed(job_hash: str, user_id: int) -> bool:
    """Check if a job has already been processed for this user."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT 1 FROM processed_jobs WHERE job_hash = ? AND user_id = ?",
            (job_hash, user_id)
        ) as cursor:
            return await cursor.fetchone() is not None


async def log_processed_job(job_hash: str, user_id: int) -> None:
    """Log a processed job for deduplication."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO processed_jobs (job_hash, user_id) VALUES (?, ?)",
            (job_hash, user_id)
        )
        await db.commit()
        logger.debug(f"Job logged: hash={job_hash[:8]}... user={user_id}")


async def clear_processed_jobs(user_id: int) -> int:
    """Clear all processed jobs for a user. Returns count deleted."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "DELETE FROM processed_jobs WHERE user_id = ?",
            (user_id,)
        )
        await db.commit()
        count = cursor.rowcount
        logger.info(f"Cleared {count} processed jobs for user {user_id}")
        return count


async def get_jobs_last_24h(user_id: int) -> list:
    """Get all processed jobs from the last 24 hours for reporting."""
    cutoff = datetime.now() - timedelta(hours=24)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT job_hash, found_at 
            FROM processed_jobs 
            WHERE user_id = ? AND found_at >= ?
            ORDER BY found_at DESC
            """,
            (user_id, cutoff.isoformat())
        ) as cursor:
            rows = await cursor.fetchall()
        return [dict(row) for row in rows]


# ============== STATISTICS ==============

async def get_db_stats() -> dict:
    """Get database statistics for health check."""
    async with aiosqlite.connect(DB_PATH) as db:
        stats = {}
        
        async with db.execute("SELECT COUNT(*) FROM users") as cursor:
            stats["users"] = (await cursor.fetchone())[0]
        
        async with db.execute("SELECT COUNT(*) FROM processed_jobs") as cursor:
            stats["processed_jobs"] = (await cursor.fetchone())[0]
        
        async with db.execute("SELECT COUNT(*) FROM monitored_urls") as cursor:
            stats["monitored_urls"] = (await cursor.fetchone())[0]
        
        return stats


# ============== MONITORED URL OPERATIONS ==============

async def add_monitored_url(user_id: int, url: str, label: str) -> int:
    """Add a monitored URL for a user. Returns the new ID."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            INSERT INTO monitored_urls (user_id, url, label)
            VALUES (?, ?, ?)
            """,
            (user_id, url, label.upper())
        )
        await db.commit()
        new_id = cursor.lastrowid
        logger.info(f"Monitored URL added: id={new_id} user={user_id} label={label.upper()}")
        return new_id


async def get_monitored_urls(user_id: int) -> list:
    """Get all monitored URLs for a user."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT id, url, label, created_at FROM monitored_urls WHERE user_id = ?",
            (user_id,)
        ) as cursor:
            rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def get_all_monitored_urls() -> list:
    """Get all monitored URLs from all users (for scheduled scan)."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT id, user_id, url, label FROM monitored_urls"
        ) as cursor:
            rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def delete_monitored_url(user_id: int, url_id: int) -> bool:
    """Delete a monitored URL by ID. Returns True if deleted."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "DELETE FROM monitored_urls WHERE id = ? AND user_id = ?",
            (url_id, user_id)
        )
        await db.commit()
        deleted = cursor.rowcount > 0
        if deleted:
            logger.info(f"Monitored URL deleted: id={url_id} user={user_id}")
        return deleted

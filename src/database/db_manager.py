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
                rss_url TEXT
            )
        """)
        
        # CV slots table with unique constraint per user+label
        await db.execute("""
            CREATE TABLE IF NOT EXISTS cv_slots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER REFERENCES users(telegram_id) ON DELETE CASCADE,
                label TEXT NOT NULL,
                content TEXT NOT NULL,
                UNIQUE(user_id, label)
            )
        """)
        
        # Processed jobs table with score and matching label
        await db.execute("""
            CREATE TABLE IF NOT EXISTS processed_jobs (
                job_hash TEXT,
                user_id INTEGER REFERENCES users(telegram_id) ON DELETE CASCADE,
                score INTEGER,
                matching_label TEXT,
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


async def get_user_data(telegram_id: int) -> Optional[dict]:
    """Get user info with all associated CVs."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        
        # Get user info
        async with db.execute(
            "SELECT telegram_id, name, rss_url FROM users WHERE telegram_id = ?",
            (telegram_id,)
        ) as cursor:
            user_row = await cursor.fetchone()
            
        if not user_row:
            return None
        
        # Get all CVs for user
        async with db.execute(
            "SELECT label, content FROM cv_slots WHERE user_id = ?",
            (telegram_id,)
        ) as cursor:
            cv_rows = await cursor.fetchall()
        
        return {
            "telegram_id": user_row["telegram_id"],
            "name": user_row["name"],
            "rss_url": user_row["rss_url"],
            "cvs": {row["label"]: row["content"] for row in cv_rows}
        }


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


# ============== CV OPERATIONS ==============

async def add_cv(telegram_id: int, label: str, content: str) -> None:
    """Add or update a CV slot for a user."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO cv_slots (user_id, label, content)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id, label) DO UPDATE SET content = excluded.content
            """,
            (telegram_id, label.upper(), content)
        )
        await db.commit()
        logger.info(f"CV '{label.upper()}' added/updated for user {telegram_id}")


async def delete_cv(telegram_id: int, label: str) -> bool:
    """Delete a specific CV slot. Returns True if deleted, False if not found."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "DELETE FROM cv_slots WHERE user_id = ? AND label = ?",
            (telegram_id, label.upper())
        )
        await db.commit()
        deleted = cursor.rowcount > 0
        if deleted:
            logger.info(f"CV '{label.upper()}' deleted for user {telegram_id}")
        return deleted


async def get_all_cvs(telegram_id: int) -> dict:
    """Get all CV slots for a user as {label: content} dict."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT label, content FROM cv_slots WHERE user_id = ?",
            (telegram_id,)
        ) as cursor:
            rows = await cursor.fetchall()
        return {row["label"]: row["content"] for row in rows}


# ============== JOB OPERATIONS ==============

async def check_job_processed(job_hash: str, user_id: int) -> bool:
    """Check if a job has already been processed for this user."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT 1 FROM processed_jobs WHERE job_hash = ? AND user_id = ?",
            (job_hash, user_id)
        ) as cursor:
            return await cursor.fetchone() is not None


async def log_processed_job(
    job_hash: str, 
    user_id: int, 
    score: int, 
    matching_label: str
) -> None:
    """Log a processed job with its score and matching CV label."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT OR IGNORE INTO processed_jobs (job_hash, user_id, score, matching_label)
            VALUES (?, ?, ?, ?)
            """,
            (job_hash, user_id, score, matching_label)
        )
        await db.commit()
        logger.info(f"Job logged: hash={job_hash[:8]}... user={user_id} score={score}")


async def get_jobs_last_24h(user_id: int) -> list:
    """Get all processed jobs from the last 24 hours for reporting."""
    cutoff = datetime.now() - timedelta(hours=24)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT job_hash, score, matching_label, found_at 
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
        
        async with db.execute("SELECT COUNT(*) FROM cv_slots") as cursor:
            stats["cv_slots"] = (await cursor.fetchone())[0]
        
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

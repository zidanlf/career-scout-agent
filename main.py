"""
Career Scout Agent - Main Orchestrator
Runs the Telegram bot with 1-minute rotation job scanning scheduler.
Includes RSS feed and URL monitoring support.
Scrape & notify only — no AI analysis.
"""

import asyncio
import logging
import os
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

from dotenv import load_dotenv

# Add src to path
sys.path.insert(0, str(Path(__file__).parent))

from src.database.db_manager import (
    init_db, 
    get_user_rss,
    get_all_users,
    get_user_keywords,
    log_processed_job,
    get_all_monitored_urls,
    check_job_processed,
    clean_old_jobs,
)
from src.scrapers.rss_parser import get_fresh_jobs
from src.scrapers.web_scraper import scrape_job_listings
from src.notifications.bot_handler import create_bot_application

# Load environment variables
load_dotenv()

# Configure logging
LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "scout.log"),
        logging.StreamHandler()
    ]
)

logger = logging.getLogger("main")

# User IDs from environment
ZIDAN_ID = int(os.getenv("ZIDAN_ID", "0"))
PARTNER_ID = int(os.getenv("PARTNER_ID", "0"))
 
# Scan interval in seconds (1 minute — near real-time)
SCAN_INTERVAL = 60

# Global state for scheduling information
SCAN_STATE = {
    "last_run_time": None,
    "interval_minutes": SCAN_INTERVAL // 60,
}

# Global state to track which monitored URL index was last scanned for each user (URL Rotation)
MONITOR_INDEX_STATE = {}

# Timezone: WIB (UTC+7) - always use this for consistent scheduling
WIB = timezone(timedelta(hours=7))


def clean_company_name(company: str) -> str:
    """
    Clean company name from Jobstreet 'di' prefix.
    e.g. 'diPT YAKIN BERTUMBUH SEKURITAS' -> 'PT YAKIN BERTUMBUH SEKURITAS'
    """
    if not company:
        return company
    # Strip leading 'di' prefix (Jobstreet prepends 'di' = Indonesian "at")
    # Match 'di' followed by uppercase letter (e.g. diPT, diCV, diBank)
    cleaned = re.sub(r'^di(?=[A-Z])', '', company)
    return cleaned.strip()


def format_job_notification(job: dict) -> str:
    """
    Format a job notification message with aligned colons in a pre box.
    Returns HTML-formatted string.
    """
    title = job.get('title', 'No Title')
    company = clean_company_name(job.get('company', 'Unknown Company'))
    link = job.get('link', '#')
    platform = job.get('platform', 'unknown').capitalize()
    
    # Build the pre-formatted box with aligned colons
    text = (
        f"<b>Job Found in {platform}!</b>\n\n"
        f"<pre>"
        f"Role    : {title}\n"
        f"Company : {company}"
        f"</pre>\n\n"
        f"<a href=\"{link}\">Apply Here</a>"
    )
    
    return text


def job_matches_keywords(title: str, keywords: list[str]) -> bool:
    """
    Check if a job title matches any of the user's keywords.
    Case-insensitive partial match.
    If keywords list is empty, matches everything (no filter).
    """
    if not keywords:
        return True  # No keywords set = show all jobs
    title_lower = title.lower()
    return any(kw in title_lower for kw in keywords)


async def process_user_jobs(user_id: int, bot_app, keywords: list[str] = None) -> None:
    """
    Process jobs for a single user from RSS feed.
    Fetches RSS and sends notifications for matching jobs.
    """
    logger.info(f"Starting RSS job scan for user {user_id}")
    
    rss_url = await get_user_rss(user_id)
    
    if not rss_url:
        logger.info(f"User {user_id} has no RSS URL configured")
        return
    
    # Fetch keywords if not provided
    if keywords is None:
        keywords = await get_user_keywords(user_id)
    
    # Fetch fresh jobs
    jobs = await get_fresh_jobs(rss_url, user_id)
    
    if not jobs:
        logger.info(f"No new jobs found for user {user_id}")
        return
    
    logger.info(f"Found {len(jobs)} new jobs for user {user_id}")
    
    sent = 0
    filtered = 0
    for job in jobs:
        try:
            # Skip if already processed (prevent duplicate notifications)
            if await check_job_processed(job["hash"], user_id):
                continue

            await log_processed_job(job["hash"], user_id)
            
            if job_matches_keywords(job.get("title", ""), keywords):
                await send_notification(bot_app, user_id, job)
                sent += 1
            else:
                filtered += 1
        except Exception as e:
            logger.error(f"Error processing job '{job['title'][:50]}...': {e}")
    
    logger.info(f"RSS scan done for user {user_id}: {sent} notified, {filtered} filtered")


async def process_monitored_urls(bot_app, user_id: int = None, keywords: list[str] = None) -> dict:
    """
    Process monitored URLs. 
    Returns summary dict: {total_scraped, new_sent, already_processed, filtered, errors}
    """
    logger.info(f"=== Starting monitored URL scan {'for user ' + str(user_id) if user_id else '(All Users)'} ===")
    
    summary = {"total_scraped": 0, "new_sent": 0, "already_processed": 0, "filtered": 0, "errors": 0}
    
    # Get monitored URLs
    from src.database.db_manager import get_monitored_urls
    
    if user_id:
        monitored_urls = await get_monitored_urls(user_id)
    else:
        monitored_urls = await get_all_monitored_urls()
    
    if not monitored_urls:
        logger.info("No monitored URLs to process")
        return summary
    
    # URL Rotation: process only ONE monitored URL per user per run cycle
    if user_id:
        idx = MONITOR_INDEX_STATE.get(user_id, 0)
        idx = idx % len(monitored_urls)
        selected_url_data = monitored_urls[idx]
        MONITOR_INDEX_STATE[user_id] = idx + 1
        
        logger.info(f"Rotating URLs for user {user_id}: processing URL {idx + 1}/{len(monitored_urls)}")
        urls_to_process = [selected_url_data]
    else:
        logger.info(f"Processing {len(monitored_urls)} monitored URLs")
        urls_to_process = monitored_urls
    
    # If keywords not provided and user_id given, fetch from DB
    if keywords is None and user_id:
        keywords = await get_user_keywords(user_id)
    elif keywords is None:
        keywords = []
    
    for url_data in urls_to_process:
        current_user_id = user_id if user_id else url_data.get("user_id")
        url = url_data["url"]
        
        # If scanning all users, get keywords per user
        if not user_id:
            user_keywords = await get_user_keywords(current_user_id)
        else:
            user_keywords = keywords
        
        try:
            logger.info(f"Scraping URL for user {current_user_id}: {url[:50]}...")
            
            # Scrape the URL
            jobs = await scrape_job_listings(url)
            
            if not jobs:
                logger.info(f"No jobs found from URL: {url[:50]}...")
                continue
            
            summary["total_scraped"] += len(jobs)
            logger.info(f"Found {len(jobs)} jobs from URL")
            
            # Collect new (unprocessed) jobs
            new_jobs = []
            for job in jobs:
                if not await check_job_processed(job["hash"], current_user_id):
                    new_jobs.append(job)
            
            already = len(jobs) - len(new_jobs)
            summary["already_processed"] += already
            
            if not new_jobs:
                logger.info(f"No new jobs from URL: {url[:50]}...")
                continue
            
            logger.info(f"{len(new_jobs)} new jobs from URL")
            
            # Send all new jobs as notifications (with keyword filter)
            for job in new_jobs:
                try:
                    # Always log as processed (prevents re-checking)
                    await log_processed_job(job["hash"], current_user_id)
                    
                    # Only notify if matches keywords
                    if job_matches_keywords(job.get("title", ""), user_keywords):
                        await send_notification(bot_app, current_user_id, job)
                        summary["new_sent"] += 1
                    else:
                        summary["filtered"] += 1
                        logger.debug(f"Filtered out (keywords): {job.get('title', '')[:40]}")
                except Exception as e:
                    logger.error(f"Error sending notification: {e}")
                    summary["errors"] += 1
            
            logger.info(f"URL processed: {summary['new_sent']} sent, {summary['filtered']} filtered")
            
        except Exception as e:
            logger.error(f"Error processing URL {url[:50]}...: {e}")
            summary["errors"] += 1
            continue
    
    logger.info("=== Monitored URL scan completed ===")
    return summary


async def send_notification(bot_app, user_id: int, job: dict) -> None:
    """Send a job notification via Telegram."""
    try:
        text = format_job_notification(job)
        
        await bot_app.bot.send_message(
            chat_id=user_id,
            text=text,
            parse_mode="HTML",
            disable_web_page_preview=True
        )
        
        title = job.get('title', 'No Title')
        logger.info(f"Notification sent to user {user_id}: {title[:30]}...")
        
    except Exception as e:
        logger.error(f"Failed to send notification to {user_id}: {e}")


async def scheduled_scan(bot_app) -> None:
    """
    Continuous job scanner with round-robin scheduling.
    Scans ONE active user per cycle (every 60 seconds), rotating turns.
    If only 1 active user, that user gets scanned every cycle.
    """
    scan_index = 0  # Round-robin index
    
    while True:
        try:
            # Auto-clean expired jobs older than 30 days
            try:
                await clean_old_jobs()
            except Exception as e:
                logger.error(f"Failed to run periodic database cleanup: {e}")

            now = datetime.now(WIB)
            logger.info(f"=== Scheduled scan starting at {now.strftime('%H:%M:%S')} ===")
            
            # Get all registered users
            all_users = await get_all_users()
            
            if not all_users:
                logger.info("No registered users, skipping scan")
            else:
                # Filter only active users
                active_users = [u for u in all_users if u.get("active", 1)]
                skipped = len(all_users) - len(active_users)
                
                if skipped:
                    logger.info(f"Skipping {skipped} paused user(s)")
                
                if not active_users:
                    logger.info("No active users, skipping scan")
                else:
                    # Round-robin: pick one user per cycle
                    scan_index = scan_index % len(active_users)
                    user_data = active_users[scan_index]
                    
                    user_id = user_data["telegram_id"]
                    user_name = user_data.get("name", "Unknown")
                    keywords = [k.strip().lower() for k in (user_data.get("keywords") or "").split(",") if k.strip()]
                    
                    logger.info(f"--- Scanning for {user_name} (ID: {user_id}, turn: {scan_index + 1}/{len(active_users)}, keywords: {keywords or 'none'}) ---")
                    
                    # RSS Feed scan
                    if user_data.get("rss_url"):
                        try:
                            await process_user_jobs(user_id, bot_app, keywords)
                        except Exception as e:
                            logger.error(f"RSS scan error for {user_name}: {e}")
                    
                    # Monitored URL scan
                    try:
                        await process_monitored_urls(bot_app, user_id, keywords)
                    except Exception as e:
                        logger.error(f"URL scan error for {user_name}: {e}")
                    
                    # Move to next user for next cycle
                    scan_index += 1
            
            # Update scan state
            SCAN_STATE["last_run_time"] = datetime.now(WIB).strftime("%H:%M:%S")
            
        except Exception as e:
            logger.error(f"Scheduled scan error: {e}")
        
        # Wait for next scan
        logger.info(f"Next scan in {SCAN_INTERVAL} seconds...")
        await asyncio.sleep(SCAN_INTERVAL)


async def main() -> None:
    """Main entry point."""
    logger.info("=" * 50)
    logger.info("Career Scout Agent Starting...")
    logger.info("=" * 50)
    
    # Validate environment
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not bot_token:
        logger.error("TELEGRAM_BOT_TOKEN not set!")
        return
    
    # Initialize database
    await init_db()
    
    # Auto-clean expired jobs older than 30 days on startup
    try:
        await clean_old_jobs()
    except Exception as e:
        logger.error(f"Failed to run startup database cleanup: {e}")
    
    # Create bot application
    app = create_bot_application(bot_token)
    
    # Start the bot and scheduler
    async with app:
        await app.start()
        logger.info("Bot started successfully!")
        
        # Run scheduled scan in background
        scheduler_task = asyncio.create_task(scheduled_scan(app))
        
        # Start polling for updates
        await app.updater.start_polling(drop_pending_updates=True)
        logger.info("Bot is now polling for updates...")
        
        # Keep running until interrupted
        try:
            await scheduler_task
        except asyncio.CancelledError:
            pass
        finally:
            await app.updater.stop()
            await app.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        raise

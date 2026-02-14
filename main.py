"""
Career Scout Agent - Main Orchestrator
Runs the Telegram bot with hourly job scanning scheduler.
Includes RSS feed and URL monitoring support.
Scrape & notify only — no AI analysis.
"""

import asyncio
import logging
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

from dotenv import load_dotenv

# Add src to path
sys.path.insert(0, str(Path(__file__).parent))

from src.database.db_manager import (
    init_db, 
    get_user_rss,
    log_processed_job,
    get_all_monitored_urls,
    check_job_processed,
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
 
# Global state for scheduling information
SCAN_STATE = {
    "next_run_time": None,
    "next_user": "Unknown",
    "last_run_time": None
}

# Timezone: WIB (UTC+7) - always use this for consistent scheduling
WIB = timezone(timedelta(hours=7))


async def process_user_jobs(user_id: int, bot_app) -> None:
    """
    Process jobs for a single user from RSS feed.
    Fetches RSS and sends notifications for all new jobs.
    """
    logger.info(f"Starting RSS job scan for user {user_id}")
    
    rss_url = await get_user_rss(user_id)
    
    if not rss_url:
        logger.info(f"User {user_id} has no RSS URL configured")
        return
    
    # Fetch fresh jobs
    jobs = await get_fresh_jobs(rss_url, user_id)
    
    if not jobs:
        logger.info(f"No new jobs found for user {user_id}")
        return
    
    logger.info(f"Found {len(jobs)} new jobs for user {user_id}")
    
    sent = 0
    for job in jobs:
        try:
            await log_processed_job(job["hash"], user_id)
            await send_notification(bot_app, user_id, job)
            sent += 1
        except Exception as e:
            logger.error(f"Error processing job '{job['title'][:50]}...': {e}")
    
    logger.info(f"RSS scan done for user {user_id}: {sent}/{len(jobs)} jobs notified")


async def process_monitored_urls(bot_app, user_id: int = None) -> None:
    """
    Process monitored URLs. 
    If user_id is provided, only process URLs for that user.
    Otherwise, process all monitored URLs.
    """
    logger.info(f"=== Starting monitored URL scan {'for user ' + str(user_id) if user_id else '(All Users)'} ===")
    
    # Get monitored URLs
    from src.database.db_manager import get_monitored_urls
    
    if user_id:
        monitored_urls = await get_monitored_urls(user_id)
    else:
        monitored_urls = await get_all_monitored_urls()
    
    if not monitored_urls:
        logger.info("No monitored URLs to process")
        return
    
    logger.info(f"Processing {len(monitored_urls)} monitored URLs")
    
    for url_data in monitored_urls:
        current_user_id = user_id if user_id else url_data.get("user_id")
        url = url_data["url"]
        
        try:
            logger.info(f"Scraping URL for user {current_user_id}: {url[:50]}...")
            
            # Scrape the URL
            jobs = await scrape_job_listings(url)
            
            if not jobs:
                logger.info(f"No jobs found from URL: {url[:50]}...")
                continue
            
            logger.info(f"Found {len(jobs)} jobs from URL")
            
            # Collect new (unprocessed) jobs
            new_jobs = []
            for job in jobs:
                if not await check_job_processed(job["hash"], current_user_id):
                    new_jobs.append(job)
            
            if not new_jobs:
                logger.info(f"No new jobs from URL: {url[:50]}...")
                continue
            
            logger.info(f"{len(new_jobs)} new jobs from URL")
            
            # Send all new jobs as notifications
            for job in new_jobs:
                try:
                    await log_processed_job(job["hash"], current_user_id)
                    await send_notification(bot_app, current_user_id, job)
                except Exception as e:
                    logger.error(f"Error sending notification: {e}")
            
            logger.info(f"URL processed: {len(new_jobs)} new jobs sent")
            
        except Exception as e:
            logger.error(f"Error processing URL {url[:50]}...: {e}")
            continue
    
    logger.info("=== Monitored URL scan completed ===")


async def send_notification(bot_app, user_id: int, job: dict) -> None:
    """Send a job notification via Telegram."""
    try:
        title = job.get('title', 'No Title')
        company = job.get('company', 'Unknown Company')
        link = job.get('link', '#')
        
        text = (
            f"\U0001f4cc <b>{title}</b>\n"
            f"\U0001f3e2 {company}\n\n"
            f"<a href=\"{link}\">Apply Here \u2192</a>"
        )
        
        await bot_app.bot.send_message(
            chat_id=user_id,
            text=text,
            parse_mode="HTML",
            disable_web_page_preview=True
        )
        
        logger.info(f"Notification sent to user {user_id}: {title[:30]}...")
        
    except Exception as e:
        logger.error(f"Failed to send notification to {user_id}: {e}")


async def scheduled_scan(bot_app) -> None:
    """
    Scheduled job scanner.
    Runs every hour:
    - Process RSS feeds with user rotation
    - Process all monitored URLs
    """
    while True:
        try:
            current_hour = datetime.now(WIB).hour
            
            # === RSS Feed Scan (User Rotation) ===
            if current_hour % 2 == 0:
                target_user = ZIDAN_ID
                user_name = "ZIDAN"
            else:
                target_user = PARTNER_ID
                user_name = "PARTNER"
            
            if target_user == 0:
                logger.warning(f"{user_name}_ID not configured, skipping RSS scan")
            else:
                logger.info(f"=== RSS scan starting for {user_name} (Hour: {current_hour}) ===")
                await process_user_jobs(target_user, bot_app)
                logger.info(f"=== RSS scan completed for {user_name} ===")
            
            # === Monitored URL Scan (Unified with RSS Rotation) ===
            if target_user != 0:
                logger.info(f"=== URL scan starting for {user_name} ===")
                await process_monitored_urls(bot_app, target_user)
                logger.info(f"=== URL scan completed for {user_name} ===")
            
            # === Update Next Scan Info ===
            now = datetime.now(WIB)
            next_time = now.hour + 1
            next_user_name = "PARTNER" if next_time % 2 != 0 else "ZIDAN"
            
            SCAN_STATE["last_run_time"] = now.strftime("%H:%M:%S")
            SCAN_STATE["next_run_time"] = f"{next_time:02d}:00"
            SCAN_STATE["next_user"] = next_user_name
            
        except Exception as e:
            logger.error(f"Scheduled scan error: {e}")
        
        # Wait 60 minutes
        logger.info("Next scan in 60 minutes...")
        await asyncio.sleep(3600)


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

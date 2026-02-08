"""
Career Scout Agent - Main Orchestrator
Runs the Telegram bot with hourly job scanning scheduler.
Includes RSS feed and URL monitoring support.
"""

import asyncio
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

# Add src to path
sys.path.insert(0, str(Path(__file__).parent))

from src.database.db_manager import (
    init_db, 
    get_user_data, 
    log_processed_job,
    get_all_monitored_urls,
    get_all_cvs,
    check_job_processed,
)
from src.scrapers.rss_parser import get_fresh_jobs
from src.scrapers.web_scraper import scrape_job_listings
from src.ai.analyzer import analyze_single_job, analyze_job_fit
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


async def process_user_jobs(user_id: int, bot_app) -> None:
    """
    Process jobs for a single user from RSS feed.
    Fetches RSS, analyzes jobs, and sends notifications for high matches.
    """
    logger.info(f"Starting RSS job scan for user {user_id}")
    
    # Get user data
    user_data = await get_user_data(user_id)
    
    if not user_data:
        logger.warning(f"User {user_id} not found in database")
        return
    
    if not user_data.get("rss_url"):
        logger.info(f"User {user_id} has no RSS URL configured")
        return
    
    if not user_data.get("cvs"):
        logger.info(f"User {user_id} has no CVs stored")
        return
    
    # Fetch fresh jobs
    jobs = await get_fresh_jobs(user_data["rss_url"], user_id)
    
    if not jobs:
        logger.info(f"No new jobs found for user {user_id}")
        return
    
    logger.info(f"Found {len(jobs)} new jobs for user {user_id}")
    
    # Analyze each job
    for job in jobs:
        try:
            result = await analyze_single_job(job, user_data["cvs"])
            
            if result:
                score = result.get("score", 0)
                best_cv = result.get("best_cv", "?")
                
                # Log the processed job
                await log_processed_job(job["hash"], user_id, score, best_cv)
                
                logger.info(f"Job '{job['title'][:50]}...' scored {score} for CV '{best_cv}'")
                
                # Send notification if score > 60
                if score > 60:
                    await send_scheduled_notification(
                        bot_app, user_id, job, result
                    )
            else:
                logger.error(f"AI analysis failed for job: {job['title'][:50]}...")
                await log_processed_job(job["hash"], user_id, 0, "FAILED")
                
        except Exception as e:
            logger.error(f"Error processing job '{job['title'][:50]}...': {e}")
            await log_processed_job(job["hash"], user_id, 0, "ERROR")


async def process_monitored_urls(bot_app) -> None:
    """
    Process all monitored URLs from all users.
    Scrapes job listings, analyzes new jobs, and sends notifications.
    """
    logger.info("=== Starting monitored URL scan ===")
    
    # Get all monitored URLs
    monitored_urls = await get_all_monitored_urls()
    
    if not monitored_urls:
        logger.info("No monitored URLs to process")
        return
    
    logger.info(f"Processing {len(monitored_urls)} monitored URLs")
    
    for url_data in monitored_urls:
        user_id = url_data["user_id"]
        url = url_data["url"]
        label = url_data["label"]
        
        try:
            logger.info(f"Scraping URL for user {user_id}: {url[:50]}...")
            
            # Get user's CVs
            cvs = await get_all_cvs(user_id)
            if not cvs:
                logger.warning(f"User {user_id} has no CVs, skipping URL")
                continue
            
            # Check if specified label exists
            if label not in cvs:
                logger.warning(f"CV label '{label}' not found for user {user_id}, skipping")
                continue
            
            # Scrape the URL
            jobs = await scrape_job_listings(url)
            
            if not jobs:
                logger.info(f"No jobs found from URL: {url[:50]}...")
                continue
            
            logger.info(f"Found {len(jobs)} jobs from URL")
            
            # Process each job
            new_jobs = 0
            matches = 0
            
            for job in jobs:
                # Check if already processed
                if await check_job_processed(job["hash"], user_id):
                    continue
                
                new_jobs += 1
                
                # Analyze with specific CV based on label
                cv_for_analysis = {label: cvs[label]}
                
                try:
                    result = await analyze_job_fit(
                        f"Title: {job['title']}\n\n{job.get('description', '')}",
                        cv_for_analysis
                    )
                    
                    if result:
                        score = result.get("score", 0)
                        
                        # Log the job
                        await log_processed_job(job["hash"], user_id, score, label)
                        
                        # Send notification if score > 60
                        if score > 60:
                            await send_scheduled_notification(
                                bot_app, user_id, job, result
                            )
                            matches += 1
                    else:
                        await log_processed_job(job["hash"], user_id, 0, "FAILED")
                        
                except Exception as e:
                    logger.error(f"Error analyzing job: {e}")
                    await log_processed_job(job["hash"], user_id, 0, "ERROR")
            
            logger.info(f"URL processed: {new_jobs} new jobs, {matches} matches sent")
            
        except Exception as e:
            logger.error(f"Error processing URL {url[:50]}...: {e}")
            continue
    
    logger.info("=== Monitored URL scan completed ===")


async def send_scheduled_notification(
    bot_app, 
    user_id: int, 
    job: dict, 
    analysis: dict
) -> None:
    """Send a job notification via the bot with HTML formatting."""
    try:
        score = analysis.get("score", 0)
        best_cv = analysis.get("best_cv", "?")
        strengths = analysis.get("strengths", [])[:3]
        gaps = analysis.get("gaps", [])[:3]
        model_used = analysis.get("model_used", "Unknown")
        
        # Extract short model name
        model_short = model_used.split("/")[-1].split(":")[0] if "/" in model_used else model_used
        
        title = job.get('title', 'No Title')
        company = job.get('company', 'Unknown Company')
        
        strengths_text = "\n".join([f"- {s}" for s in strengths]) if strengths else "- None identified"
        gaps_text = "\n".join([f"- {g}" for g in gaps]) if gaps else "- None identified"
        
        text = (
            f"<b>JOB MATCH FOUND</b>\n"
            f"----------------------------------\n"
            f"<b>{title}</b>\n"
            f"{company}\n\n"
            f"TARGET LABEL : <code>{best_cv}</code>\n"
            f"MATCH SCORE  : <code>{score}/100</code>\n"
            f"AI ENGINE    : <code>{model_short}</code>\n\n"
            f"<b>KEY STRENGTHS</b>\n"
            f"{strengths_text}\n\n"
            f"<b>IDENTIFIED GAPS</b>\n"
            f"{gaps_text}\n\n"
            f"<a href=\"{job.get('link', '#')}\">Apply Here</a>"
        )
        
        await bot_app.bot.send_message(
            chat_id=user_id,
            text=text,
            parse_mode="HTML",
            disable_web_page_preview=True
        )
        
        logger.info(f"Notification sent to user {user_id} for job: {job['title'][:30]}...")
        
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
            current_hour = datetime.now().hour
            
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
            
            # === Monitored URL Scan (All Users) ===
            await process_monitored_urls(bot_app)
            
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
    
    if not os.getenv("OPENROUTER_API_KEY"):
        logger.warning("OPENROUTER_API_KEY not set - AI analysis will fail")
    
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

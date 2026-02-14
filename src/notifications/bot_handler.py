"""
Telegram Bot Handler for Career Scout Agent
Implements commands with whitelist security.
Scrape & notify only — no AI analysis.
"""

import logging
import os
from datetime import datetime, timedelta
from functools import wraps
from typing import Callable

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

from src.database.db_manager import (
    upsert_user,
    get_user_rss,
    update_rss,
    delete_rss,
    get_db_stats,
    get_jobs_last_24h,
    add_monitored_url,
    get_monitored_urls,
    delete_monitored_url,
    log_processed_job,
    check_job_processed,
    clear_processed_jobs,
)
from src.scrapers.rss_parser import get_fresh_jobs

# Configure module logger
logger = logging.getLogger(__name__)

# Authorized user IDs (loaded from env)
AUTHORIZED_IDS: set[int] = set()


def load_authorized_ids() -> None:
    """Load authorized user IDs from environment."""
    global AUTHORIZED_IDS
    zidan_id = os.getenv("ZIDAN_ID")
    partner_id = os.getenv("PARTNER_ID")
    
    if zidan_id:
        AUTHORIZED_IDS.add(int(zidan_id))
    if partner_id:
        AUTHORIZED_IDS.add(int(partner_id))
    
    logger.info(f"Loaded {len(AUTHORIZED_IDS)} authorized user(s)")


def authorized_only(func: Callable) -> Callable:
    """Decorator to restrict access to whitelisted users only."""
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        username = update.effective_user.username or "Unknown"
        
        if user_id not in AUTHORIZED_IDS:
            logger.warning(f"Unauthorized access attempt: {user_id} (@{username})")
            return  # Silent ignore
        
        return await func(update, context)
    return wrapper


# ============== COMMAND HANDLERS ==============

@authorized_only
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Register user in database."""
    user = update.effective_user
    await upsert_user(user.id, user.full_name)
    
    await update.message.reply_html(
        f"<b>Welcome, {user.first_name}!</b>\n"
        "You have been registered in Career Scout.\n\n"
        "<b>RSS Feed</b>\n"
        "/setrss &lt;url&gt; - Set RSS feed URL\n"
        "/delrss - Remove RSS feed\n"
        "/scanrss - Scan RSS feed now\n\n"
        "<b>URL Monitoring</b>\n"
        "/monitor &lt;url&gt; &lt;tag&gt; - Add URL to monitor\n"
        "/listmonitor - List monitored URLs\n"
        "/delmonitor &lt;id&gt; - Remove monitored URL\n"
        "/scanmonitor - Scan all monitored URLs now\n\n"
        "<b>Info</b>\n"
        "/next - Check next scan schedule\n"
        "/report - 24h summary\n"
        "/status - System status\n"
        "/clearjobs - Reset job history (re-discover all jobs)"
    )
    logger.info(f"User registered: {user.id} ({user.full_name})")


@authorized_only
async def cmd_setrss(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Set RSS feed URL. Usage: /setrss <url>"""
    if not context.args:
        await update.message.reply_text(
            "*Usage:* `/setrss <url>`\n"
            "*Example:* `/setrss http://localhost:3000/?action=...`",
            parse_mode="Markdown"
        )
        return
    
    url = context.args[0]
    await update_rss(update.effective_user.id, url)
    
    await update.message.reply_text(
        f"RSS feed URL configured:\n`{url[:60]}{'...' if len(url) > 60 else ''}`",
        parse_mode="Markdown"
    )


@authorized_only
async def cmd_delrss(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Remove RSS feed URL."""
    await delete_rss(update.effective_user.id)
    await update.message.reply_text("RSS feed URL removed.", parse_mode="Markdown")


@authorized_only
async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show system status and user info."""
    user_id = update.effective_user.id
    rss_url = await get_user_rss(user_id)
    db_stats = await get_db_stats()
    
    rss_status = "Configured" if rss_url else "Not configured"
    
    # Get schedule info from main.SCAN_STATE
    from main import SCAN_STATE
    last_run = SCAN_STATE.get("last_run_time", "Never")
    next_run = SCAN_STATE.get("next_run_time", "Calculating...")
    next_user = SCAN_STATE.get("next_user", "Unknown")
    
    await update.message.reply_text(
        "*System Status*\n\n"
        "*Your Profile:*\n"
        f"- RSS Feed: {rss_status}\n\n"
        "*Schedule:*\n"
        f"- Last Run: {last_run}\n"
        f"- Next Run: {next_run} ({next_user}'s RSS)\n\n"
        "*Database Statistics:*\n"
        f"- Users: {db_stats.get('users', 0)}\n"
        f"- Jobs Processed: {db_stats.get('processed_jobs', 0)}\n"
        f"- Monitored URLs: {db_stats.get('monitored_urls', 0)}",
        parse_mode="Markdown"
    )


@authorized_only
async def cmd_report(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show 24-hour job scan summary."""
    user_id = update.effective_user.id
    jobs = await get_jobs_last_24h(user_id)
    
    if not jobs:
        await update.message.reply_text(
            "*24-Hour Report*\n\n"
            "No jobs scanned in the last 24 hours.",
            parse_mode="Markdown"
        )
        return
    
    total = len(jobs)
    
    await update.message.reply_text(
        "*24-Hour Report*\n\n"
        f"Jobs found: {total}",
        parse_mode="Markdown"
    )


# ============== RSS SCAN COMMAND ==============

@authorized_only
async def cmd_scanrss(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Trigger RSS feed scan for this user."""
    user_id = update.effective_user.id
    
    rss_url = await get_user_rss(user_id)
    
    if not rss_url:
        await update.message.reply_text(
            "No RSS feed configured. Use `/setrss` first.",
            parse_mode="Markdown"
        )
        return
    
    await update.message.reply_text("Scanning RSS feed for jobs. Please wait...")
    
    # Fetch fresh jobs
    jobs = await get_fresh_jobs(rss_url, user_id)
    
    if not jobs:
        await update.message.reply_text("No new jobs found in the last 24 hours.")
        return
    
    await update.message.reply_text(f"Found {len(jobs)} new job(s). Sending notifications...")
    
    sent = 0
    for job in jobs:
        if not await check_job_processed(job["hash"], user_id):
            await log_processed_job(job["hash"], user_id)
            
            title = job.get('title', 'No Title')
            company = job.get('company', 'Unknown Company')
            link = job.get('link', '#')
            
            text = (
                f"<b>{title}</b>\n"
                f"{company}\n\n"
                f"<a href=\"{link}\">Apply Here</a>"
            )
            
            await update.message.reply_html(text, disable_web_page_preview=True)
            sent += 1
    
    await update.message.reply_text(f"Scan complete. {sent} new job(s) notified.")


@authorized_only
async def cmd_next(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the next scan schedule info."""
    from main import SCAN_STATE, WIB
    
    last_run = SCAN_STATE.get("last_run_time")
    next_run = SCAN_STATE.get("next_run_time")
    next_user = SCAN_STATE.get("next_user")
    
    if not next_run:
        now = datetime.now(WIB)
        next_hour = now.hour + 1
        target_user = "PARTNER" if next_hour % 2 != 0 else "ZIDAN"
        next_run = f"{next_hour:02d}:00"
        next_user = target_user

    # Calculate countdown
    now = datetime.now(WIB)
    try:
        next_hour_val = int(next_run.split(":")[0])
        minutes_left = (next_hour_val * 60) - (now.hour * 60 + now.minute)
        if minutes_left < 0: minutes_left += 24 * 60 
        
        countdown = f"{minutes_left // 60}h {minutes_left % 60}m"
    except:
        countdown = "Calculating..."

    text = (
        "<b>NEXT SCAN SCHEDULE</b>\n\n"
        f"<b>Last Run:</b>  {last_run or 'None'}\n"
        f"<b>Next Run:</b>  {next_run}\n"
        f"<b>Target:</b>    {next_user}\n"
        f"<b>Remaining:</b> {countdown}\n\n"
        "<i>Note: Both RSS Feed and Monitored URLs for the target user will be scanned.</i>"
    )
    
    await update.message.reply_html(text)


# ============== URL MONITORING COMMANDS ==============

@authorized_only
async def cmd_monitor(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Add a URL to monitor for job listings.
    Usage: /monitor <URL> <TAG>
    """
    user_id = update.effective_user.id
    
    if not context.args or len(context.args) < 2:
        await update.message.reply_html(
            "<b>Usage:</b> <code>/monitor &lt;URL&gt; &lt;TAG&gt;</code>\n\n"
            "<b>Example:</b>\n"
            "<code>/monitor https://www.kalibrr.com/c/jobs?search=data%20engineer DE</code>\n\n"
            "<b>Supported platforms:</b>\n"
            "- Kalibrr\n"
            "- LinkedIn\n"
            "- Glints"
        )
        return
    
    url = context.args[0]
    label = context.args[1].upper()
    
    # Validate URL
    if not url.startswith("http"):
        await update.message.reply_text("Invalid URL. Must start with http:// or https://")
        return
    
    # Add to database
    new_id = await add_monitored_url(user_id, url, label)
    
    await update.message.reply_html(
        f"<b>URL Added to Monitoring</b>\n\n"
        f"<pre>"
        f"ID    : {new_id}\n"
        f"TAG   : {label}\n"
        f"URL   : {url[:50]}{'...' if len(url) > 50 else ''}"
        f"</pre>\n"
        f"This URL will be scanned hourly."
    )
    
    logger.info(f"User {user_id} added monitored URL: id={new_id} tag={label}")


@authorized_only
async def cmd_delmonitor(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Delete a monitored URL by ID.
    Usage: /delmonitor <ID>
    """
    user_id = update.effective_user.id
    
    if not context.args:
        await update.message.reply_html(
            "<b>Usage:</b> <code>/delmonitor &lt;ID&gt;</code>\n\n"
            "Use <code>/listmonitor</code> to see your monitored URLs with their IDs."
        )
        return
    
    try:
        url_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Invalid ID. Must be a number.")
        return
    
    deleted = await delete_monitored_url(user_id, url_id)
    
    if deleted:
        await update.message.reply_html(f"Monitored URL with ID <code>{url_id}</code> deleted.")
    else:
        await update.message.reply_html(f"URL with ID <code>{url_id}</code> not found.")


@authorized_only
async def cmd_scanmonitor(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Trigger a manual scan of your monitored URLs."""
    user_id = update.effective_user.id
    await update.message.reply_text("⚙️ Scanning your monitored URLs...")
    
    from main import process_monitored_urls
    
    summary = await process_monitored_urls(context.application, user_id=user_id)
    
    # Show detailed summary
    total_scraped = summary.get("total_scraped", 0)
    new_sent = summary.get("new_sent", 0)
    already_processed = summary.get("already_processed", 0)
    errors = summary.get("errors", 0)
    
    text = (
        "<b>Scan Complete</b>\n\n"
        f"Jobs scraped: {total_scraped}\n"
        f"New jobs sent: {new_sent}\n"
        f"Already processed: {already_processed}\n"
    )
    if errors:
        text += f"Errors: {errors}\n"
    
    if total_scraped == 0:
        text += "\n<i>Tip: scraper returned 0 jobs. Check if the URL is still valid.</i>"
    elif new_sent == 0 and already_processed > 0:
        text += "\n<i>Tip: all jobs were already seen. Use /clearjobs to reset and rescan.</i>"
    
    await update.message.reply_html(text)


@authorized_only
async def cmd_listmonitor(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List all monitored URLs for the user."""
    user_id = update.effective_user.id
    
    urls = await get_monitored_urls(user_id)
    
    if not urls:
        await update.message.reply_html(
            "No monitored URLs. Use <code>/monitor</code> to add one."
        )
        return
    
    lines = ["<b>Your Monitored URLs</b>\n---------------------------"]
    for url_data in urls:
        lines.append(
            f"\nID    : <code>{url_data['id']}</code>\n"
            f"TAG   : <code>{url_data['label']}</code>\n"
            f"URL   : {url_data['url']}"
        )
    
    await update.message.reply_html("\n".join(lines))


@authorized_only
async def cmd_clearjobs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Clear all processed jobs so they can be re-discovered."""
    user_id = update.effective_user.id
    count = await clear_processed_jobs(user_id)
    await update.message.reply_html(
        f"<b>Job history cleared</b>\n\n"
        f"Deleted {count} processed job records.\n"
        f"Run <code>/scanmonitor</code> to re-scan and get fresh notifications."
    )


# ============== APPLICATION FACTORY ==============

def create_bot_application(token: str) -> Application:
    """Create and configure the Telegram bot application."""
    load_authorized_ids()
    
    app = Application.builder().token(token).build()
    
    # Register command handlers
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("setrss", cmd_setrss))
    app.add_handler(CommandHandler("delrss", cmd_delrss))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("report", cmd_report))
    app.add_handler(CommandHandler("scanrss", cmd_scanrss))
    app.add_handler(CommandHandler("next", cmd_next))
    app.add_handler(CommandHandler("monitor", cmd_monitor))
    app.add_handler(CommandHandler("scanmonitor", cmd_scanmonitor))
    app.add_handler(CommandHandler("delmonitor", cmd_delmonitor))
    app.add_handler(CommandHandler("listmonitor", cmd_listmonitor))
    app.add_handler(CommandHandler("clearjobs", cmd_clearjobs))
    
    logger.info("Bot application created with all handlers")
    return app

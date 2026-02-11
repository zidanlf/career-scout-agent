"""
Telegram Bot Handler for Career Scout Agent
Implements all CRUD commands with whitelist security.
Supports stateful CV upload via .txt files and manual job scanning.
"""

import logging
import os
from datetime import datetime, timedelta
from functools import wraps
from typing import Callable

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from src.database.db_manager import (
    init_db,
    upsert_user,
    get_user_data,
    update_rss,
    delete_rss,
    add_cv,
    delete_cv,
    get_all_cvs,
    get_db_stats,
    get_jobs_last_24h,
    add_monitored_url,
    get_monitored_urls,
    delete_monitored_url,
)
from src.scrapers.rss_parser import get_fresh_jobs
from src.ai.analyzer import analyze_single_job, analyze_job_fit
from src.database.db_manager import log_processed_job

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
        "<b>CV Management</b>\n"
        "/addcv &lt;label&gt; &lt;text&gt; - Add CV content\n"
        "/delcv &lt;label&gt; - Delete a CV\n"
        "/listcv - List your CVs\n\n"
        "<b>RSS Feed</b>\n"
        "/setrss &lt;url&gt; - Set RSS feed URL\n"
        "/delrss - Remove RSS feed\n"
        "/scanrss - Scan RSS feed now\n\n"
        "<b>URL Monitoring</b>\n"
        "/monitor &lt;url&gt; &lt;label&gt; - Add URL to monitor\n"
        "/listmonitor - List monitored URLs\n"
        "/delmonitor &lt;id&gt; - Remove monitored URL\n"
        "/scanmonitor - Scan all monitored URLs now\n\n"
        "<b>Analysis</b>\n"
        "/scan &lt;label&gt; &lt;text&gt; - Manual job analysis\n"
        "/next - Check next scan schedule\n"
        "/report - 24h summary\n"
        "/status - System status\n\n"
        "<b>Upload CV via File</b>\n"
        "Send a .txt file, then provide the label."
    )
    logger.info(f"User registered: {user.id} ({user.full_name})")


@authorized_only
async def cmd_addcv(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Add or update a CV slot. Usage: /addcv <label> <text>"""
    if not context.args or len(context.args) < 2:
        await update.message.reply_text(
            "*Usage:* `/addcv <label> <cv_text>`\n"
            "*Example:* `/addcv AI Experienced in machine learning...`",
            parse_mode="Markdown"
        )
        return
    
    label = context.args[0].upper()
    content = " ".join(context.args[1:])
    
    await add_cv(update.effective_user.id, label, content)
    
    await update.message.reply_text(
        f"CV `{label}` saved successfully.\n"
        f"Content: {content[:100]}{'...' if len(content) > 100 else ''}",
        parse_mode="Markdown"
    )


@authorized_only
async def cmd_delcv(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Delete a CV slot. Usage: /delcv <label>"""
    if not context.args:
        await update.message.reply_text(
            "*Usage:* `/delcv <label>`\n"
            "*Example:* `/delcv AI`",
            parse_mode="Markdown"
        )
        return
    
    label = context.args[0].upper()
    deleted = await delete_cv(update.effective_user.id, label)
    
    if deleted:
        await update.message.reply_text(f"CV `{label}` deleted successfully.", parse_mode="Markdown")
    else:
        await update.message.reply_text(f"CV `{label}` not found.", parse_mode="Markdown")


@authorized_only
async def cmd_listcv(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List all CV slots for the user."""
    cvs = await get_all_cvs(update.effective_user.id)
    
    if not cvs:
        await update.message.reply_text(
            "No CVs stored. Use `/addcv` to add one or upload a .txt file.",
            parse_mode="Markdown"
        )
        return
    
    lines = ["*Your CVs:*\n"]
    for label, content in cvs.items():
        preview = content[:80] + "..." if len(content) > 80 else content
        lines.append(f"- `{label}`: {preview}")
    
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


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
    user_data = await get_user_data(user_id)
    db_stats = await get_db_stats()
    
    rss_status = "Configured" if user_data and user_data.get("rss_url") else "Not configured"
    cv_count = len(user_data.get("cvs", {})) if user_data else 0
    
    # Get schedule info from main.SCAN_STATE
    from main import SCAN_STATE
    last_run = SCAN_STATE.get("last_run_time", "Never")
    next_run = SCAN_STATE.get("next_run_time", "Calculating...")
    next_user = SCAN_STATE.get("next_user", "Unknown")
    
    await update.message.reply_text(
        "*System Status*\n\n"
        "*Your Profile:*\n"
        f"- RSS Feed: {rss_status}\n"
        f"- CVs Stored: {cv_count}\n\n"
        "*Schedule:*\n"
        f"- Last Run: {last_run}\n"
        f"- Next Run: {next_run} ({next_user}'s RSS)\n\n"
        "*Database Statistics:*\n"
        f"- Users: {db_stats.get('users', 0)}\n"
        f"- CV Slots: {db_stats.get('cv_slots', 0)}\n"
        f"- Jobs Processed: {db_stats.get('processed_jobs', 0)}",
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
    
    # Calculate stats
    total = len(jobs)
    scores = [j["score"] for j in jobs if j.get("score")]
    avg_score = sum(scores) / len(scores) if scores else 0
    high_matches = [j for j in jobs if j.get("score", 0) > 60]
    
    # Top matches
    top_3 = sorted(jobs, key=lambda x: x.get("score", 0), reverse=True)[:3]
    top_lines = []
    for j in top_3:
        top_lines.append(f"- Score {j.get('score', 0)} - CV `{j.get('matching_label', '?')}`")
    
    await update.message.reply_text(
        "*24-Hour Report*\n\n"
        "*Summary:*\n"
        f"- Jobs Scanned: {total}\n"
        f"- Average Score: {avg_score:.1f}\n"
        f"- High Matches (>60): {len(high_matches)}\n\n"
        "*Top Matches:*\n" + "\n".join(top_lines),
        parse_mode="Markdown"
    )


# ============== MANUAL SCAN COMMAND ==============

@authorized_only
async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Manual job scan with specific CV label.
    Usage: /scan <LABEL> <job_description_text>
    """
    user_id = update.effective_user.id
    
    if not context.args or len(context.args) < 2:
        await update.message.reply_text(
            "*Usage:* `/scan <LABEL> <job_description>`\n\n"
            "*Example:*\n"
            "`/scan DE Looking for Data Engineer with 3+ years Python experience...`\n\n"
            "The command will analyze the job against your CV with the specified label.",
            parse_mode="Markdown"
        )
        return
    
    label = context.args[0].upper()
    job_text = " ".join(context.args[1:])
    
    # Get CV from database
    cvs = await get_all_cvs(user_id)
    
    if not cvs:
        await update.message.reply_text(
            "No CVs stored. Use `/addcv` to add one first.",
            parse_mode="Markdown"
        )
        return
    
    if label not in cvs:
        available = ", ".join([f"`{l}`" for l in cvs.keys()])
        await update.message.reply_text(
            f"CV with label `{label}` not found.\n\n"
            f"*Available CVs:* {available}",
            parse_mode="Markdown"
        )
        return
    
    await update.message.reply_text("Analyzing job description. Please wait...")
    
    # Analyze with single CV
    cv_content = {label: cvs[label]}
    result = await analyze_job_fit(job_text, cv_content)
    
    if not result:
        await update.message.reply_text(
            "Analysis failed. AI service may be unavailable. Please try again later."
        )
        return
    
    # Format output
    score = result.get("score", 0)
    strengths = result.get("strengths", [])[:3]  # Top 3
    gaps = result.get("gaps", [])[:3]  # Top 3
    model_used = result.get("model_used", "Unknown")
    
    # Extract short model name
    model_short = model_used.split("/")[-1].split(":")[0] if "/" in model_used else model_used
    
    strengths_text = "\n".join([f"  - {s}" for s in strengths]) if strengths else "  - None identified"
    gaps_text = "\n".join([f"  - {g}" for g in gaps]) if gaps else "  - None identified"
    
    text = (
        f"<b>ANALYSIS REPORT</b>\n\n"
        f"<pre>"
        f"LABEL : {label}\n"
        f"SCORE : {score}/100\n"
        f"MODEL : {model_short}\n\n"
        f"KEY STRENGTHS\n"
        f"{strengths_text}\n\n"
        f"IDENTIFIED GAPS\n"
        f"{gaps_text}"
        f"</pre>"
    )
    
    await update.message.reply_html(text)
    
    logger.info(f"Manual scan completed for user {user_id}: CV={label}, Score={score}")


# ============== RSS SCAN COMMAND ==============

@authorized_only
async def cmd_scanrss(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Trigger RSS feed scan for this user."""
    user_id = update.effective_user.id
    
    # Get user data
    user_data = await get_user_data(user_id)
    
    if not user_data or not user_data.get("rss_url"):
        await update.message.reply_text(
            "No RSS feed configured. Use `/setrss` first.",
            parse_mode="Markdown"
        )
        return
    
    if not user_data.get("cvs"):
        await update.message.reply_text(
            "No CVs stored. Use `/addcv` first.",
            parse_mode="Markdown"
        )
        return
    
    await update.message.reply_text("Scanning RSS feed for jobs. Please wait...")
    
    # Fetch fresh jobs
    jobs = await get_fresh_jobs(user_data["rss_url"], user_id)
    
    if not jobs:
        await update.message.reply_text("No new jobs found in the last 24 hours.")
        return
    
    await update.message.reply_text(f"Found {len(jobs)} new job(s). Analyzing...")
    
    # Analyze each job
    matches_sent = 0
    for job in jobs:
        result = await analyze_single_job(job, user_data["cvs"])
        
        if result:
            score = result.get("score", 0)
            best_cv = result.get("best_cv", "?")
            
            # Log the job
            await log_processed_job(job["hash"], user_id, score, best_cv)
            
            # Send notification if score > 60
            if score > 60:
                await send_job_notification(update, job, result)
                matches_sent += 1
        else:
            # Log failed analysis with score 0
            await log_processed_job(job["hash"], user_id, 0, "FAILED")
    
    await update.message.reply_text(
        f"Scan complete.\n"
        f"- Analyzed: {len(jobs)} jobs\n"
        f"- Matches sent: {matches_sent}"
    )


@authorized_only
async def cmd_next(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the next scan schedule info."""
    from main import SCAN_STATE, WIB
    
    last_run = SCAN_STATE.get("last_run_time")
    next_run = SCAN_STATE.get("next_run_time")
    next_user = SCAN_STATE.get("next_user")
    
    if not next_run:
        # If bot just started and haven't run yet, calculate estimated
        now = datetime.now(WIB)
        next_hour = now.hour + 1
        target_user = "PARTNER" if next_hour % 2 != 0 else "ZIDAN"
        next_run = f"{next_hour:02d}:00"
        next_user = target_user

    # Calculate countdown
    now = datetime.now(WIB)
    try:
        next_hour_val = int(next_run.split(":")[0])
        # Simple countdown logic for same-day
        minutes_left = (next_hour_val * 60) - (now.hour * 60 + now.minute)
        # If negative, it's for tomorrow or just now (ignore complexity for now)
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


# ============== STATEFUL DOCUMENT HANDLER (CV via .txt file) ==============

@authorized_only
async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handle CV upload via .txt file (Step 1 of stateful flow).
    Stores content in user_data and prompts for label.
    """
    user_id = update.effective_user.id
    document = update.message.document
    
    # Check file extension
    file_name = document.file_name or ""
    if not file_name.lower().endswith(".txt"):
        await update.message.reply_text(
            "Invalid file format. Only .txt files are accepted for CV upload."
        )
        return
    
    try:
        # Download file
        file = await document.get_file()
        file_bytes = await file.download_as_bytearray()
        
        # Decode to string
        content = file_bytes.decode("utf-8")
        
        if not content.strip():
            await update.message.reply_text("File is empty. Please upload a valid CV file.")
            return
        
        # Store content in user_data for stateful handling
        context.user_data["pending_cv_content"] = content
        
        await update.message.reply_text(
            "CV file received.\n\n"
            "Please reply with the label you want to assign to this CV.\n"
            "*Example:* `DE` or `AI`",
            parse_mode="Markdown"
        )
        
        logger.info(f"CV file received from user {user_id}, waiting for label. Size: {len(content)} chars")
        
    except UnicodeDecodeError:
        await update.message.reply_text(
            "Failed to read file. Please ensure the file is a valid UTF-8 encoded text file."
        )
    except Exception as e:
        logger.error(f"Error processing document upload: {e}")
        await update.message.reply_text(
            "An error occurred while processing the file. Please try again."
        )


@authorized_only
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handle text messages (Step 2 of stateful flow).
    If there's a pending CV, use the message as the label.
    """
    user_id = update.effective_user.id
    text = update.message.text
    
    # Check if there's a pending CV waiting for a label
    pending_content = context.user_data.get("pending_cv_content")
    
    if pending_content:
        # Use the message text as the label
        label = text.strip().upper()
        
        if not label:
            await update.message.reply_text(
                "Invalid label. Please provide a non-empty label for your CV."
            )
            return
        
        # Validate label (alphanumeric only, max 10 chars)
        if not label.isalnum() or len(label) > 10:
            await update.message.reply_text(
                "Invalid label format. Use alphanumeric characters only, max 10 characters.\n"
                "*Example:* `DE`, `AI`, `UIUX`",
                parse_mode="Markdown"
            )
            return
        
        # Save to database
        await add_cv(user_id, label, pending_content)
        
        # Clear the pending content
        del context.user_data["pending_cv_content"]
        
        await update.message.reply_text(
            f"Success. Your CV has been saved under the label: `{label}`\n"
            f"Content length: {len(pending_content)} characters",
            parse_mode="Markdown"
        )
        
        logger.info(f"CV saved for user {user_id}: label={label}, size={len(pending_content)}")
    else:
        # No pending CV, ignore or provide help
        # This prevents the bot from responding to every random message
        pass


# ============== NOTIFICATION HELPER ==============

async def send_job_notification(
    update: Update, 
    job: dict, 
    analysis: dict
) -> None:
    """Send a job notification to the user with minimalist HTML formatting."""
    score = analysis.get("score", 0)
    best_cv = analysis.get("best_cv", "?")
    strengths = analysis.get("strengths", [])[:3]  # Top 3
    gaps = analysis.get("gaps", [])[:3]  # Top 3
    model_used = analysis.get("model_used", "Unknown")
    
    # Extract short model name
    model_short = model_used.split("/")[-1].split(":")[0] if "/" in model_used else model_used
    
    title = job.get('title', 'No Title')
    company = job.get('company', 'Unknown Company')
    
    strengths_text = "\n".join([f"  - {s}" for s in strengths]) if strengths else "  - None identified"
    gaps_text = "\n".join([f"  - {g}" for g in gaps]) if gaps else "  - None identified"
    
    text = (
        f"<b>JOB MATCH FOUND</b>\n\n"
        f"<b>{title}</b>\n"
        f"{company}\n\n"
        f"<pre>"
        f"LABEL : {best_cv}\n"
        f"SCORE : {score}/100\n"
        f"MODEL : {model_short}\n\n"
        f"KEY STRENGTHS\n"
        f"{strengths_text}\n\n"
        f"IDENTIFIED GAPS\n"
        f"{gaps_text}"
        f"</pre>"
    )
    
    # Add apply button
    keyboard = [[InlineKeyboardButton("Apply Now", url=job.get("link", "#"))]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_html(text, reply_markup=reply_markup)


# ============== URL MONITORING COMMANDS ==============

@authorized_only
async def cmd_monitor(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Add a URL to monitor for job listings.
    Usage: /monitor <URL> <LABEL>
    """
    user_id = update.effective_user.id
    
    if not context.args or len(context.args) < 2:
        await update.message.reply_html(
            "<b>Usage:</b> <code>/monitor &lt;URL&gt; &lt;LABEL&gt;</code>\n\n"
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
        f"LABEL : {label}\n"
        f"URL   : {url[:50]}{'...' if len(url) > 50 else ''}"
        f"</pre>\n"
        f"This URL will be scanned hourly."
    )
    
    logger.info(f"User {user_id} added monitored URL: id={new_id} label={label}")


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
    await update.message.reply_text("Starting manual scan of your monitored URLs. Please wait...")
    
    # Import here to avoid circular dependency
    from main import process_monitored_urls
    
    # Run the orchestration for ONLY this user
    await process_monitored_urls(context.application, user_id=user_id)
    
    await update.message.reply_text("Monitored URL scan completed. Check matches above.")


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
        url_preview = url_data['url'][:40] + "..." if len(url_data['url']) > 40 else url_data['url']
        lines.append(
            f"\nID    : <code>{url_data['id']}</code>\n"
            f"LABEL : <code>{url_data['label']}</code>\n"
            f"URL   : {url_preview}"
        )
    
    await update.message.reply_html("\n".join(lines))


# ============== APPLICATION FACTORY ==============

def create_bot_application(token: str) -> Application:
    """Create and configure the Telegram bot application."""
    load_authorized_ids()
    
    app = Application.builder().token(token).build()
    
    # Register command handlers
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("addcv", cmd_addcv))
    app.add_handler(CommandHandler("delcv", cmd_delcv))
    app.add_handler(CommandHandler("listcv", cmd_listcv))
    app.add_handler(CommandHandler("setrss", cmd_setrss))
    app.add_handler(CommandHandler("delrss", cmd_delrss))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("report", cmd_report))
    app.add_handler(CommandHandler("scan", cmd_scan))
    app.add_handler(CommandHandler("scanrss", cmd_scanrss))
    app.add_handler(CommandHandler("next", cmd_next))
    app.add_handler(CommandHandler("monitor", cmd_monitor))
    app.add_handler(CommandHandler("scanmonitor", cmd_scanmonitor))
    app.add_handler(CommandHandler("delmonitor", cmd_delmonitor))
    app.add_handler(CommandHandler("listmonitor", cmd_listmonitor))
    
    # Register document handler for .txt CV uploads (stateful step 1)
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    
    # Register text message handler for label capture (stateful step 2)
    # Only captures plain text that is not a command
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    logger.info("Bot application created with all handlers")
    return app


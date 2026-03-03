# Career Scout Agent

Telegram bot that automatically scrapes job listings from multiple platforms and sends real-time notifications. Supports scheduled hourly scans with multi-user rotation.

## Features

- **Multi-platform scraping** — LinkedIn, Jobstreet, Dealls, and generic sites
- **RSS feed monitoring** via RSS-Bridge (self-hosted)
- **URL monitoring** — track specific search result pages for new listings
- **Scheduled hourly scans** with user rotation (even hours / odd hours)
- **Telegram bot interface** with whitelist security
- **Deduplication** — only notifies for new job listings

## Supported Platforms

| Platform | Method | Notes |
|----------|--------|-------|
| LinkedIn | HTML scraping | Public job listings only |
| Jobstreet | SEEK Search API + HTML fallback | Lightweight, no browser needed |

## Prerequisites

- Python 3.10+
- A Telegram Bot Token (from [@BotFather](https://t.me/BotFather))
- RSS-Bridge instance (self-hosted, for RSS feed support)

## Setup

### 1. Clone and install

```bash
git clone https://github.com/zidanlf/career-scout-agent.git
cd career-scout-agent
python -m venv venv
source venv/bin/activate        # Linux/Mac
# venv\Scripts\activate         # Windows
pip install -r requirements.txt
```

### 2. Configure environment

Create a `.env` file in the project root:

```env
# Telegram Bot
TELEGRAM_BOT_TOKEN=your_bot_token_here

# Authorized User IDs (whitelist)
ZIDAN_ID=your_telegram_user_id
OTHER_USER_ID=other_user_telegram_id

# RSS-Bridge (optional, for /setrss)
RSS_BRIDGE_URL=http://localhost:3000
```

### 3. Run

```bash
python main.py
```

## Bot Commands

### RSS Feed

| Command | Description |
|---------|-------------|
| `/start` | Register and show help |
| `/setrss <url>` | Set RSS feed URL |
| `/delrss` | Remove RSS feed |
| `/scanrss` | Scan RSS feed now |

### URL Monitoring

| Command | Description |
|---------|-------------|
| `/monitor <url> <tag>` | Add a URL to monitor |
| `/listmonitor` | List all monitored URLs |
| `/delmonitor <id>` | Remove a monitored URL |
| `/scanmonitor` | Scan all monitored URLs now |

### Info & Utilities

| Command | Description |
|---------|-------------|
| `/next` | Show next scan schedule |
| `/report` | 24-hour job summary |
| `/status` | System status |
| `/clearjobs` | Reset job history (re-discover all jobs) |

## Notification Format

```
Job Found in Jobstreet!

Role    : Data Engineer
Exp     : Fresh Graduate
Company : PT Tokopedia

Apply Here
```

Each notification includes the platform name, job role, experience level (extracted from description), company name, and a clickable apply link. The information is displayed in a monospace box with aligned colons for readability.

## Scheduling

The bot scans automatically every hour with user rotation:

- **Even hours** (00, 02, 04, ...) → User A's RSS + monitored URLs
- **Odd hours** (01, 03, 05, ...) → User B's RSS + monitored URLs

Use `/next` to check the upcoming scan schedule.

## Project Structure

```
career-scout-agent/
├── main.py                     # Entry point, scheduler, orchestrator
├── requirements.txt            # Python dependencies
├── .env                        # Credentials (not in git)
├── data/
│   └── scout.db                # SQLite database
├── logs/
│   └── scout.log               # Application logs
└── src/
    ├── database/
    │   └── db_manager.py       # SQLite operations (users, jobs, URLs)
    ├── scrapers/
    │   ├── rss_parser.py       # RSS feed parser
    │   └── web_scraper.py      # Multi-platform job scraper
    └── notifications/
        └── bot_handler.py      # Telegram bot commands & handlers
```

## Database Tables

| Table | Purpose |
|-------|---------|
| `users` | Registered users with RSS URL |
| `processed_jobs` | Deduplication tracking (job_hash + user_id) |
| `monitored_urls` | Saved search URLs for periodic scanning |

## Deployment (systemd)

Create `/etc/systemd/system/career-scout.service`:

```ini
[Unit]
Description=Career Scout Agent
After=network.target

[Service]
Type=simple
User=your_user
WorkingDirectory=/home/your_user/career-scout-agent
ExecStart=/home/your_user/career-scout-agent/venv/bin/python main.py
Restart=always
RestartSec=10
Environment=PATH=/home/your_user/career-scout-agent/venv/bin:/usr/bin

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable career-scout
sudo systemctl start career-scout
```

## Dependencies

| Package | Purpose |
|---------|---------|
| `python-telegram-bot` | Telegram Bot API |
| `aiosqlite` | Async SQLite database |
| `httpx` | HTTP client for API/HTML scraping |
| `beautifulsoup4` | HTML parsing |
| `lxml` | Fast HTML parser backend |
| `python-dotenv` | Environment variable loading |

## License

MIT


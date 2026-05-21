# Career Scout Agent

Telegram bot that automatically scrapes job listings from multiple platforms and sends real-time notifications. Supports scheduled 1-minute rotation scans with multi-user round-robin.

## Features

- **Multi-platform scraping** — LinkedIn, Jobstreet, Dealls, Glints, and Kalibrr
- **RSS feed monitoring** via RSS-Bridge (self-hosted)
- **URL monitoring** — track specific search result pages for new listings
- **Scheduled 1-minute rotation scans** with user round-robin rotation
- **Telegram bot interface** with whitelist security
- **Deduplication** — only notifies for new job listings

## Supported Platforms

| Platform | Method | Notes |
|----------|--------|-------|
| LinkedIn | HTML scraping | Public job listings only |
| Jobstreet | TLS impersonation + HTML | curl_cffi for Cloudflare bypass |
| Dealls | HTML scraping | Standard HTML parsing |
| Glints | TLS impersonation + HTML | curl_cffi for anti-bot bypass |
| Kalibrr | HTML scraping | Server-side rendered, no anti-bot |

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

### Keyword Filtering

| Command | Description |
|---------|-------------|
| `/setkeywords <k1>, <k2>` | Set role filter keywords |
| `/listkeywords` | Show active keywords |
| `/delkeywords` | Remove all keywords |

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
| `/status` | System status + keywords |
| `/report` | 24-hour job summary |
| `/clearjobs` | Reset job history (re-discover all jobs) |

## Notification Format

```
Job Found in Jobstreet!

Role    : Data Engineer
Company : PT Tokopedia

Apply Here
```

Each notification includes the platform name, job role, company name, and a clickable apply link. The information is displayed in a monospace box with aligned colons for readability.

## Keyword Filtering

Users can set keywords to filter job notifications by role title:

```
/setkeywords data, etl, engineer
```

Only jobs whose title contains at least one keyword will trigger a notification. Matching is **case-insensitive** and **partial** (e.g. keyword `data` matches "Data Engineer", "Big Data Analyst"). If no keywords are set, all jobs are shown.

## Scheduling

The bot runs a continuous scanner every **1 minute** with round-robin rotation:

- **User Rotation**: Picks one active user per cycle (every 60 seconds).
- **URL Rotation**: For the selected user, scans exactly **one monitored URL** in rotation per cycle.
- **RSS Scan**: Scrapes the user's RSS feed.
- **Filtering & Notification**: Applies keyword filtering and sends notifications for new (unseen) jobs only.

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


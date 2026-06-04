# Career Scout Agent (Rust Version)

A high-performance, lightweight Telegram bot rewritten in pure Rust that automatically scrapes job listings from multiple platforms and sends real-time notifications. Featuring structured round-robin scheduling, whitelist security, and database volume persistence.

## Features

- **High Performance & Safety** — Built in Rust, offering extremely low memory and CPU footprint compared to the legacy Python implementation.
- **Multi-platform scraping** — LinkedIn, Jobstreet, Glints, Kalibrr, Indeed, Loker.id, and KitaLulus.
- **RSS Feed Parser** — Parse and monitor personalized RSS feeds with fallback title filtering.
- **URL Monitoring** — Monitor specific search result pages from target job boards for new listings.
- **Keyword Filtering** — Restrict job alerts to specific roles matching user-defined keywords (case-insensitive, partial matching).
- **Control Commands** — `/pause` and `/resume` scanning dynamically per user.
- **Whitelist Protection** — Restrict bot access exclusively to authorized Telegram IDs.
- **Deduplication** — Prevent duplicate notifications using MD5 job hashing and SQLite persistence.
- **Optimized Scheduling** — Periodic scans (default: 2 minutes) executing round-robin rotation for active users with a 2-second rate-limiting delay between network requests.

---

## Supported Platforms

| Platform | Method | Bypass / Parser |
|----------|--------|-----------------|
| **LinkedIn** | HTML Scraping | Standard HTTP & HTML Selector Parsing |
| **Jobstreet** | HTML Scraping | Custom Web Scraping via Scraper Crate |
| **Glints** | HTML Scraping | Custom Web Scraping via Scraper Crate |
| **Kalibrr** | HTML Scraping | Standard HTML Parser |
| **Indeed** | HTML Scraping | Custom Web Scraping via Scraper Crate |
| **Loker.id** | HTML Scraping | Standard HTML Selector Parser |
| **KitaLulus** | HTML Scraping | Standard HTML Selector Parser |

---

## Prerequisites

- **Rust toolchain** (Rust 1.88+ or latest stable)
- **Docker** (for containerized deployment)
- **Telegram Bot Token** (from [@BotFather](https://t.me/BotFather))

---

## Setup & Local Development

### 1. Clone the Repository
```bash
git clone https://github.com/zidanlf/career-scout-agent.git
cd career-scout-agent
```

### 2. Configure the Environment
Create a `.env` file in the root directory:
```env
# Telegram Bot Token
TELEGRAM_BOT_TOKEN=your_bot_token_here

# Authorized User IDs (comma-separated whitelist)
ZIDAN_ID=123456789,987654321

# Scan interval in seconds (default: 120 / 2 minutes)
SCAN_INTERVAL=120
```

### 3. Run Locally
```bash
cargo run --release
```

To run unit tests:
```bash
cargo test
```

---

## Deployment (Docker)

The project includes a multi-stage `Dockerfile` that builds a highly secure and optimized Debian-slim container.

### 1. Build the Docker Image
```bash
docker build -t career-bot .
```

### 2. Run Container with Volume Persistence
To ensure that your SQLite database (`scout.db`) and logs are preserved across updates, mount the `data` and `logs` directories:
```bash
docker run -d \
  --name career-scout-running \
  --restart always \
  --env-file .env \
  -v "$(pwd)/data:/app/data" \
  -v "$(pwd)/logs:/app/logs" \
  career-bot
```

---

## Bot Commands

### Keyword Filtering
| Command | Description | Example |
|---------|-------------|---------|
| `/setkeywords <k1>, <k2>` | Set role filter keywords | `/setkeywords rust, python, remote` |
| `/listkeywords` | View active keywords | `/listkeywords` |
| `/delkeywords` | Delete all keywords | `/delkeywords` |

### RSS Feed Settings
| Command | Description | Example |
|---------|-------------|---------|
| `/setrss <url>` | Set your RSS feed URL | `/setrss https://rss.example.com/feed` |
| `/delrss` | Delete RSS feed | `/delrss` |
| `/scanrss` | Scan RSS feed immediately | `/scanrss` |

### URL Monitoring
| Command | Description | Example |
|---------|-------------|---------|
| `/monitor <url> <tag>` | Monitor a search URL with a label | `/monitor https://jobstreet.co.id/... backend` |
| `/listmonitor` | List monitored URLs | `/listmonitor` |
| `/delmonitor <id>` | Delete monitored URL by ID | `/delmonitor 3` |
| `/scanmonitor` | Scan monitored URLs immediately | `/scanmonitor` |

### System & Control
| Command | Description | Example |
|---------|-------------|---------|
| `/start` | Register and show welcome guide | `/start` |
| `/status` | View scan status & active keywords | `/status` |
| `/report` | Get 24-hour job posting summary | `/report` |
| `/clearjobs` | Reset job history database | `/clearjobs` |
| `/pause` | Pause automatic scanning loop | `/pause` |
| `/resume` | Resume automatic scanning loop | `/resume` |

---

## Project Structure

```
career-scout-agent/
├── Cargo.toml          # Rust project configuration & dependencies
├── Cargo.lock          # Locked versions of dependencies
├── Dockerfile          # Multi-stage Docker deployment build
├── README.md           # Documentation
├── .env                # Secret environment variables (not in git)
├── data/
│   └── scout.db        # SQLite database (persisted via docker volume)
├── logs/
│   └── scout.log       # Application logs (persisted via docker volume)
└── src/
    ├── main.rs         # Entry point, config loaders, scheduler loop
    ├── bot.rs          # Teloxide bot commands & telegram response handling
    ├── database.rs     # SQLx async database queries & migrations
    ├── scraper.rs      # HTML selectors, parsers, and scraper client
    └── config.rs       # Environment configuration loading & WIB time utils
```

---

## License

This project is licensed under the MIT License.

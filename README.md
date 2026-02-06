# Career Scout Agent

Autonomous job scout agent that monitors RSS feeds, analyzes job-CV fit using AI, and sends Telegram notifications for matching opportunities.

## Features

- Multi-CV support with custom labels
- RSS feed monitoring (via RSS-Bridge)
- AI-powered job-CV matching with 3-tier model fallback
- Telegram bot interface with whitelist security
- Scheduled hourly scans with user rotation

## Setup

1. Clone the repository:
```bash
git clone https://github.com/YOUR_USERNAME/career-scout-agent.git
cd career-scout-agent
```

2. Create virtual environment:
```bash
python -m venv venv
venv\Scripts\activate  # Windows
# source venv/bin/activate  # Linux/Mac
```

3. Install dependencies:
```bash
pip install -r requirements.txt
```

4. Create `.env` file with your credentials:
```env
TELEGRAM_BOT_TOKEN=your_telegram_bot_token
OPENROUTER_API_KEY=your_openrouter_api_key
ZIDAN_ID=your_telegram_user_id
PARTNER_ID=partner_telegram_user_id
RSS_BRIDGE_URL=http://localhost:3000
```

5. Run the bot:
```bash
python main.py
```

## Bot Commands

| Command | Description |
|---------|-------------|
| `/start` | Register and show help |
| `/addcv <label> <text>` | Add CV via text |
| `/delcv <label>` | Delete a CV |
| `/listcv` | List all CVs |
| `/setrss <url>` | Set RSS feed URL |
| `/delrss` | Remove RSS feed |
| `/scan <label> <job_text>` | Manual job analysis |
| `/scanrss` | Scan RSS feed for jobs |
| `/report` | 24-hour summary |
| `/status` | System status |

**Upload CV via File:** Send a `.txt` file, then provide the label when prompted.

## Project Structure

```
career-scout-agent/
├── main.py              # Entry point & orchestrator
├── requirements.txt     # Dependencies
├── .env                 # Credentials (not in git!)
└── src/
    ├── database/        # SQLite database manager
    ├── scrapers/        # RSS parser
    ├── ai/              # OpenRouter AI analyzer
    └── notifications/   # Telegram bot handler
```

## License

MIT

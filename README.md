# 🎾 Islington Tennis Court Monitor

A web app that monitors the Islington Tennis Centre booking page 24/7 and emails you when courts become available.

**Live dashboard** at `http://localhost:5050` — shows all slots across the week, grouped by date, with real-time updates.

![How it works]
1. Playwright (headless Chrome) loads each day's booking page
2. Parses "N spaces available" for every time slot
3. When a previously full slot becomes available → emails you + shows it on the dashboard
4. Dashboard auto-refreshes every 5 seconds

## Quick Start

```bash
# 1. Install dependencies
pip install -r requirements.txt
playwright install chromium

# 2. Run
python app.py

# 3. Open http://localhost:5050
```

Then click ⚙ **Settings** in the dashboard to configure your email and preferred times. Click **Start Monitoring** and you're done.

## Gmail Setup

1. Go to https://myaccount.google.com/apppasswords
2. Create a new app password
3. Paste the 16-character code in Settings → Gmail App Password

## Configuration

All settings are configurable from the web dashboard (⚙ Settings button). They're saved to `config.json`.

| Setting | Description |
|---------|-------------|
| Email | Where to send alerts |
| App Password | Gmail app password (not your real password) |
| Check every | How often to scan (1–5 minutes) |
| Days ahead | How many days to check (3–14) |
| Preferred times | Only alert for these times (empty = all) |

## Hosting on Your Website

### Simple: Run on a VPS

```bash
# SSH into your server
ssh user@your-server.com

# Install
git clone <your-repo> tennis-monitor
cd tennis-monitor
pip install -r requirements.txt
playwright install chromium

# Run with screen (persists after SSH disconnect)
screen -S tennis
python app.py
# Ctrl+A, D to detach

# Optional: reverse proxy with nginx
# server { location / { proxy_pass http://localhost:5050; } }
```

### Docker (optional)

```dockerfile
FROM python:3.11-slim
RUN pip install flask playwright apscheduler
RUN playwright install chromium && playwright install-deps
WORKDIR /app
COPY . .
EXPOSE 5050
CMD ["python", "app.py"]
```

## Files

```
tennis-monitor/
├── app.py              # Flask server + Playwright scraper
├── static/
│   └── index.html      # Dashboard frontend
├── config.json         # Settings (auto-created)
├── state.json          # Persisted state (auto-created)
├── requirements.txt
└── README.md
```

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/` | GET | Dashboard |
| `/api/status` | GET | Current slots, stats, logs |
| `/api/config` | GET/POST | Read/update settings |
| `/api/start` | POST | Start monitoring |
| `/api/stop` | POST | Stop monitoring |
| `/api/check` | POST | Trigger immediate scan |

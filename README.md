# X Monitor

Personal Windows desktop application that monitors X (Twitter) profiles and sends a native toast notification whenever a watched user publishes a new post.

**Read-only. No actions are ever taken on X.**

---

## Features

- Monitors up to ~25 public X profiles continuously
- Detects new posts within ~60 seconds
- Windows 11 toast notification with an **Open Post** button
- Single headless Chromium instance shared across workers (low RAM)
- SQLite persistence — no duplicate notifications across restarts
- Automatic worker restart on browser crash or network failure
- Live terminal status table (uptime, checks, new posts, errors)

---

## Requirements

- Windows 11
- Python 3.10+

---

## Setup

```bash
# 1. Install Python dependencies
pip install -r requirements.txt

# 2. Install Chromium (one-time, ~120 MB)
python -m playwright install chromium

# 3. Add the usernames you want to monitor in config.py
#    Edit the MONITORED_USERS list

# 4. Run
python main.py
```

---

## Configuration

All settings are in [config.py](config.py). Every value can be overridden with an environment variable:

| Setting | Default | Env var |
|---|---|---|
| `CHECK_INTERVAL_SECONDS` | `45` | `MONITOR_INTERVAL` |
| `WORKER_COUNT` | `3` | `MONITOR_WORKERS` |
| `HEADLESS` | `True` | `MONITOR_HEADLESS=false` |
| `PAGE_TIMEOUT_MS` | `20000` | `MONITOR_PAGE_TIMEOUT_MS` |
| `MAX_RETRIES` | `2` | `MONITOR_MAX_RETRIES` |
| `NOTIFICATION_SOUND` | `True` | `MONITOR_SOUND=false` |
| `DB_PATH` | `data/monitor.db` | `MONITOR_DB_PATH` |

### Adding / removing users

Edit the `MONITORED_USERS` list in [config.py](config.py):

```python
MONITORED_USERS = [
    "elonmusk",
    "OpenAI",
    "satyanadella",
    # add more here
]
```

---

## Project structure

```
twiter_agent/
├── main.py          # Entry point, signal handlers, component wiring
├── config.py        # All constants and env-var overrides
├── models.py        # UserRecord, PostInfo, MonitorResult dataclasses
├── logger.py        # Rich console + rotating file logging
├── database.py      # Async SQLite (aiosqlite)
├── browser.py       # Playwright browser pool (1 browser, N pages)
├── monitor.py       # X profile scraper
├── notifier.py      # Windows toast notifications (winotify)
├── scheduler.py     # Async worker pool
├── utils.py         # Helpers: chunk_list, uptime, status table
├── requirements.txt
├── data/            # SQLite database (auto-created)
└── logs/            # Rotating log files (auto-created)
```

---

## How it works

```
Scheduler splits 25 users across 3 workers
Each worker loops:
  for each user:
    BrowserPool.lease() → borrow one of 3 shared pages
    navigate to x.com/<username>
    find highest-ID status link matching /<username>/status/<id>
    compare with DB
    if changed → send toast → update DB
    release page
  sleep remainder of CHECK_INTERVAL
```

---

## Stopping

Press **Ctrl-C** in the terminal. The app drains in-progress checks and exits cleanly.

---

## Logs

Logs are written to `logs/monitor.log` (rotates at 10 MB, keeps 5 files).

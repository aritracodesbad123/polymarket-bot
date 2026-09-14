# polymarket-bot

Paper-trading bot for Polymarket. `live_execution` stays false: live CLOB orders are not placed.

## Setup

```bash
cp .env.example .env
# put GEMINI_API_KEY in .env
python3 main.py --check
python3 main.py --probe
```

## Run

```bash
PYTHONUNBUFFERED=1 AI_SCAN_LIMIT=2 python3 main.py --hours 6 --fresh
python3 main.py --status
```

Kill:

```bash
kill "$(cat paper_bot.pid)"
```

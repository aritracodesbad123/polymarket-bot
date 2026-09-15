# POLYGROK

Autonomous Polymarket bot. Starts in **paper trading**. Live CLOB orders stay locked until you unlock them after seven full days.

Grok estimates probabilities. Python, risk limits, and a fail-closed broker decide whether anything is sent.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
# set XAI_API_KEY for Grok; leave PRIVATE_KEY unset
python -m app.cli status
python -m app.cli run
```

Legacy `main.py` is the old Gemini paper bot. Do not extend it. Its clock does not count toward the 7-day lock.

## Docs

- [docs/TECHNICAL_NOTES.md](docs/TECHNICAL_NOTES.md)
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
- [docs/OPERATIONS.md](docs/OPERATIONS.md)

## Tests

```bash
pytest
```

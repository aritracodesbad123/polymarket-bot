# POLYGROK

Autonomous Polymarket trading bot. **Paper-first**: real market data and AI estimates, simulated fills, fail-closed live lock. Live CLOB orders stay locked until you unlock them after **seven full days** of this process — day count, env flags, or a restart alone never go live.

Grok (xAI) estimates probabilities when credits allow; on hard xAI failure the bot cascades to Vertex Gemini with tighter edge, then abstains if the cascade is exhausted. Python risk gates and a fail-closed broker decide whether anything is sent.

---

## What you get

| Piece | Role |
|---|---|
| `app/` | Trading loop: markets → research → estimate → strategy/risk → paper/live broker |
| `ops-console/` | Local ops dashboard (FastAPI **and** Streamlit) with live CLOB mark-to-market |
| `scripts/` | macOS LaunchAgent install for 24/7 paper trading |
| `docs/` | Architecture, operations, model-fallback notes |

**Do not extend** legacy root `main.py` (old Gemini paper bot). Its clock does **not** count toward the 7-day live lock. Use `python -m app.cli …` only.

---

## Requirements

- macOS (LaunchAgent + ops console paths are Mac-oriented)
- Python **3.11+**
- Network access to Polymarket (Gamma/CLOB) and your AI providers
- Optional: [cloudflared](https://developers.cloudflare.com/cloudflare-one/connections/connect-apps/) or ngrok to **share** the console URL

---

## Quick start (bot)

```bash
git clone https://github.com/aritracodesbad123/polymarket-bot.git
cd polymarket-bot

python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

cp .env.example .env
# Edit .env — at minimum set AI keys for paper (see below).
# Leave PRIVATE_KEY / POLYMARKET_PRIVATE_KEY unset until you intend to unlock live.

python -m app.cli status
python -m app.cli run
```

### Important `.env` knobs

| Variable | Purpose |
|---|---|
| `XAI_API_KEY` | Grok / xAI research + estimates |
| Vertex / Gemini vars | Fallback cascade when xAI is hard-failed (see `docs/MODEL_FALLBACK_STACK.md`) |
| `UNIVERSE_TAG` / `UNIVERSE_TAGS` | e.g. `crypto` or `crypto,forex` |
| `MIN_TRADEABLE_MID` / `MAX_TRADEABLE_MID` | Skip the AI estimate when the book mid is outside this band. Defaults `0.10` / `0.90`; edges are tradeable. Reject reason `mid_outside_band`. |
| `TRADING_MODE` | `paper` (default) or `live` (still needs full unlock AND-gate) |
| `LIVE_TRADING_ENABLED` | Must be `true` **and** activation phrase logged to unlock live |
| `POLYMARKET_PRIVATE_KEY` / wallet | Required only for live |

Paper works **without** a wallet key. Never commit `.env`.

---

## CLI cheat sheet

```bash
python -m app.cli status
python -m app.cli run
python -m app.cli markets
python -m app.cli opportunities
python -m app.cli positions
python -m app.cli orders
python -m app.cli open-marks          # live CLOB mids for open tickets
python -m app.cli daily-report
python -m app.cli calibration-report
python -m app.cli pre-live-report
python -m app.cli request-live-activation   # types ENABLE LIVE POLYMARKET TRADING
python -m app.cli kill                      # persist HALTED
python -m app.cli resume-paper
```

Full ops narrative: [docs/OPERATIONS.md](docs/OPERATIONS.md).

---

## 24/7 on this Mac (LaunchAgent)

```bash
./scripts/install_launch_agent.sh
launchctl print gui/$(id -u)/com.polygrok.bot | head
python -m app.cli status
```

Remove:

```bash
./scripts/uninstall_launch_agent.sh
```

Bot logs: `~/Library/Application Support/com.polygrok.bot/launchd.{out,err}.log`  
Keep the machine **plugged in**. Lid-closed on battery will sleep and pause the loop.

Ops console LaunchAgent plist (ships in-repo): `ops-console/com.polygrok.opsconsole.plist` → label `com.polygrok.opsconsole`, port **8765**.

---

## Ops console

Two UIs over the **same** `build_snapshot()` data (SQLite + live CLOB books for open positions):

| UI | URL | Best for |
|---|---|---|
| **FastAPI** (original dark console) | http://127.0.0.1:8765/ | Full layout, LaunchAgent install |
| **Streamlit** | http://127.0.0.1:8501/ | Shareable-feeling dashboard, easy charts |

Both read `polygrok.db` next to the repo and refresh ~every 2s. Open tickets show **live mark / unreal / bid-ask** (not stale cycle snapshots).

### FastAPI console

```bash
cd ops-console
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
./run_console.sh
# → http://127.0.0.1:8765/
```

Or rely on LaunchAgent `com.polygrok.opsconsole` (WorkingDirectory = this `ops-console/`).

### Streamlit console

```bash
cd ops-console
source .venv/bin/activate   # same venv as above after pip install -r requirements.txt
streamlit run streamlit_app.py
# → http://127.0.0.1:8501/
```

### Sharing a link with other people

The console is **local-first**: it needs your Mac’s `polygrok.db`, LaunchAgent logs, and CLOB access. **Streamlit Community Cloud cannot see your laptop**, so deploying the repo there will not show your live book.

To share a temporary HTTPS link while your Mac is online:

```bash
# Terminal A — Streamlit (or use FastAPI on 8765)
cd ops-console && streamlit run streamlit_app.py

# Terminal B — public tunnel
brew install cloudflared    # once
./share.sh 8501             # Streamlit
# ./share.sh 8765           # FastAPI instead
```

`share.sh` prints a `https://….trycloudflare.com` (or ngrok) URL. Anyone with the link can view the dashboard **while the tunnel and console stay running**. Treat it like giving read access to your paper PnL — don’t leave tunnels up unattended on a live wallet machine.

---

## Paper → live (operator-only)

1. Seven full days with `paper_trading_started_at` set by **this** `app.cli` process  
2. `python -m app.cli pre-live-report` may print `LIVE ACTIVATION ELIGIBLE` — that alone does **not** go live  
3. `python -m app.cli request-live-activation` and type exactly: `ENABLE LIVE POLYMARKET TRADING`  
4. `TRADING_MODE=live` + `LIVE_TRADING_ENABLED=true`  
5. Wallet + private key present  
6. System not halted  

Canary caps apply on first live mode. Details: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md), [docs/OPERATIONS.md](docs/OPERATIONS.md).

Kill: `python -m app.cli kill` → HALTED, blocks new orders. `resume-paper` returns to paper only.

---

## Architecture (short)

```
MarketScanner → ResearchProvider → ProbabilityEngine (Grok → Gemini cascade)
  → StrategyEngine → RiskEngine → ExecutionEngine → Broker
                                         ├─ PaperBroker
                                         └─ LiveBroker (locked)
```

- **Risk:** exposure caps, daily loss, kill line, live AND-gate  
- **Storage:** reconstructable trail in `polygrok.db` (gitignored)  
- **Idempotency:** hashed order keys; timeout → reconcile, don’t double POST  

More: [docs/TECHNICAL_NOTES.md](docs/TECHNICAL_NOTES.md), [docs/MODEL_FALLBACK_STACK.md](docs/MODEL_FALLBACK_STACK.md).

---

## Tests

```bash
source .venv/bin/activate
pytest
```

---

## Repo layout

```
app/                 # bot package (cli, ai, risk, broker, storage, …)
ops-console/         # FastAPI + Streamlit ops UI
  server.py          # FastAPI app + build_snapshot()
  streamlit_app.py   # Streamlit UI over build_snapshot()
  share.sh           # cloudflared/ngrok helper
  run_console.sh     # uvicorn :8765
docs/                # architecture & ops
scripts/             # LaunchAgent installers
tests/
reports/             # generated reports (mostly gitignored)
```

---

## Safety

- Secrets never logged, never sent to model prompts as keys  
- Live path is fail-closed; paper is the default  
- Ops tunnel = read-ish visibility into your paper book — share deliberately  
- This is trading software. You can lose money. Paper first.

# POLYGROK operations

```bash
cp .env.example .env   # add XAI_API_KEY for Grok; no private key required for paper
python -m app.cli run
python -m app.cli status
python -m app.cli markets
python -m app.cli opportunities
python -m app.cli positions
python -m app.cli orders
python -m app.cli daily-report
python -m app.cli calibration-report
python -m app.cli pre-live-report
python -m app.cli request-live-activation
python -m app.cli kill
python -m app.cli resume-paper
```

Paper works without `PRIVATE_KEY`. Do not put a wallet key in `.env` until you intend to unlock live.

## First 7 days

The bot trades only through `PaperBroker`. Real books, real Grok, simulated fills.
`python -m app.cli pre-live-report` may print `LIVE ACTIVATION ELIGIBLE` after 7 days. That does not go live.

## Unlocking live canary (operator only)

1. Seven full days of this process (`paper_trading_started_at` in `polygrok.db`)
2. `python -m app.cli request-live-activation` and type `ENABLE LIVE POLYMARKET TRADING`
3. `TRADING_MODE=live`
4. `LIVE_TRADING_ENABLED=true`
5. `POLYMARKET_PRIVATE_KEY` (or `PRIVATE_KEY`) and `POLYMARKET_WALLET_ADDRESS`
6. System not halted

Canary caps: $5/order, $20/day, 3 open positions. Autonomous inside those caps.

## Kill

`python -m app.cli kill` persists HALTED, blocks new orders, attempts live cancels if live was on.
`resume-paper` returns to paper only.

## Secrets

Never logged. Never sent to Grok. `.env` is gitignored.

## 24/7 on this Mac

Install (auto-start on login, restart on crash, caffeinate while plugged in):

```bash
./scripts/install_launch_agent.sh
```

Stop / remove:

```bash
./scripts/uninstall_launch_agent.sh
```

Logs: `~/Library/Application Support/com.polygrok.bot/launchd.{{out,err}}.log`

Keep the MacBook **plugged in**. Lid-closed on battery will still sleep and pause the bot.

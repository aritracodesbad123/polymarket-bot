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
`resume-paper` returns to paper only. It does **not** reset the weekly equity baseline or the day-scoped AI burn / realized-loss counters.

## $5,000 paper cohort (env handoff)

Do not point this cohort at an existing paper DB. Create/use a new file. Schema columns for AI burn, the weekly baseline, and daily realized P&L are added automatically on open (`ALTER TABLE` only when missing). No manual SQL. Opening the DB does not rewrite bankroll, fills, or positions.

Set exactly:

```bash
DB_PATH=polygrok-week2-5000.db
PAPER_STARTING_BANKROLL=5000
KILL_FLOOR_PCT=0.10
AI_SESSION_BUDGET_USD=10
API_DIE_CUSHION_USD=0
WEEKLY_LOSS_PCT=0.05
MAX_POSITION_USD=25
MAX_TOTAL_EXPOSURE_USD=500
MAX_DAILY_LOSS_USD=50
```

Two equity stops, both on a $5,000 start:

| Control | Env | Trips when |
|---|---|---|
| Catastrophic kill floor | `KILL_FLOOR_PCT=0.10` | equity <= **$4,500** (10% of starting bankroll). Regime DIE `kill_floor`. |
| Weekly stop | `WEEKLY_LOSS_PCT=0.05` | equity <= **$4,750** when the week baseline is $5,000 (5% of that baseline). Regime DIE `weekly_equity_stop` and HALTED. |
| No-fill AI budget | `AI_SESSION_BUDGET_USD=10` | UTC-day AI burn >= **$10** and the cohort has no fills and no open position. Regime DIE `ai_session_budget`. Does **not** HALT. |
| Post-fill screening stop | (same burn counter) | Any fill or open position exists, burn > 0, and unrealized PnL < that burn. Screening stops. Mode stays ATTACK/DEFEND. Does **not** HALT or DIE. |

The kill floor is measured against starting bankroll. The weekly stop is measured against the persisted week-start baseline. The daily loss rule is separate again: percentage `MAX_DAILY_LOSS_PCT` plus absolute `MAX_DAILY_LOSS_USD=50`, stricter one wins.

Session AI burn is the persisted UTC-day call count times `ESTIMATED_USD_PER_AI_CALL`. It survives restart and resets on the next UTC day. Zero burn does not DIE and does not stop screening, including at startup and after that rollover.

`AI_SESSION_BUDGET_USD` is the hard cap for the pre-fill path only. Once `fills` has a row, or `positions` has shares > 0, that cap is not the screening rule: new screening stops only while unrealized PnL is strictly under the session burn. Unrealized PnL at or above the burn keeps screening, even if burn is past $10. Holding review and exits keep running in every non-HALTED cycle, including after `ai_session_budget` DIE and after the post-fill screening stop. A Gamma scan error does not skip that review.

`API_DIE_CUSHION_USD` is only the DEFEND band (equity below start by the cushion, unrealized worse than minus the cushion, or burn past half of profit+cushion). It is not the spend budget. Do not set it to `10` to buy hunt time. `0` turns the DEFEND band off so a flat or slightly red book stays ATTACK until the session budget, the post-fill rule, the kill floor, or the weekly stop. `<=0` on `AI_SESSION_BUDGET_USD` disables the hard no-fill cap; the cohort value is `10`.

Leave `ESTIMATED_USD_PER_AI_CALL`, `MIN_EDGE`, and `MAX_SPREAD` at their current values. The code default for `KILL_FLOOR_PCT` remains 0.20; this cohort overrides it to 0.10. The code default for `AI_SESSION_BUDGET_USD` is 10. Gemini Survival lock is unchanged (+2¢ edge, confidence 0.50, extreme 2–98% band, quarter Kelly).

Before research or `engine.estimate`, the fresh book mid must lie in `[MIN_TRADEABLE_MID, MAX_TRADEABLE_MID]`. Unset env uses **0.10** and **0.90**. The edges are tradeable (`0.10` and `0.90` still get an estimate). A mid outside that band is rejected as `mid_outside_band` on the decision and on `system_events` (`TRADE_REJECTED`). That path does not call the model and does not increment the session AI burn. It does not change `AI_SESSION_BUDGET_USD`, the DIE/screening rules, the Gemini Survival lock, the kill floor, the weekly stop, or the absolute caps.

Week boundary is Monday 00:00 UTC. The baseline is the equity at the first cycle of that week and is stored in `system_state`. It resets only on that boundary or via `python -m app.cli reset-week-baseline` (does not clear HALTED). `resume-paper` does not move it.

AI burn is the persisted UTC-day call count times `ESTIMATED_USD_PER_AI_CALL` (not a new rate). Daily realized loss is the persisted UTC-day sum. Both fail closed if they cannot be read or written. Absolute USD caps are optional; when unset, only percentage caps apply. When set, the effective cap is the stricter of the two.

A weekly stop or daily realized-loss cap writes HALTED. The halt survives restart until `resume-paper`. After a same-day resume, the daily cap trips again if today's realized loss is still through the limit. After a same-week resume, the weekly stop trips again if equity is still at or under the baseline floor.

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

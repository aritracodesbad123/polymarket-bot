# POLYGROK architecture

Grok estimates. Python decides. Risk constrains. Execution trades.
The operator unlocks live. No per-trade approval.

```
MarketScanner → book mid band → ResearchProvider → GrokProbabilityEngine
    → StrategyEngine → RiskEngine → ExecutionEngine → Broker
                                              ├─ PaperBroker
                                              └─ LiveBroker (locked)
```

A fresh book mid outside `[MIN_TRADEABLE_MID, MAX_TRADEABLE_MID]` (default 0.10–0.90, edges included) is `mid_outside_band` before research or the probability engine, so it does not spend an AI call.

## Layers

- `app/market_data` — Gamma + CLOB public data, book walk, freshness
- `app/research` — `EvidencePacket` + `ResearchProvider` (xAI search or null)
- `app/ai` — structured `MarketEstimate` only
- `app/strategy` — edge, EV, Kelly, 21 gates
- `app/risk` — exposure, kill switch, live AND-gate authorization
- `app/broker` — paper simulation vs live CLOB
- `app/execution` — refresh book, idempotency, timeout → reconcile
- `app/storage` — SQLite reconstructable trail
- `app/evaluation` — calibration, daily, pre-live reports

## Live lock

Live submit requires all of:

1. DB `paper_trading_started_at` + 7 full days
2. Activation row with exact phrase `ENABLE LIVE POLYMARKET TRADING`
3. `LIVE_TRADING_ENABLED=true`
4. `TRADING_MODE=live`
5. Wallet + private key present
6. Not halted

Day 7, env, key, or restart alone never unlocks live. First live mode is canary.

## Paper vs live

| | Paper | Live canary |
|---|---|---|
| Market data | real | real |
| Research / Grok | real | real |
| Strategy / risk | real | real + canary caps |
| Orders | simulated on real books | CLOB V2 |

## Idempotency

`sha256(market|token|side|strategy_version|6h-window)`.
Timeout: reconcile, do not POST again.

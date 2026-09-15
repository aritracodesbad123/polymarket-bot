# PAPER TRADING READY

Date: 2026-09-15

POLYGROK is ready to run **paper trading**. It is **not** unlocked for live CLOB orders.

## What passed

- 56 pytest tests (unit, simulation, integration mapping, safety)
- Live authorization is an AND-gate: 7 paper days + exact phrase + env flags + credentials + not halted
- Day 1 live submit raises `LiveLockedError`
- `LIVE_TRADING_ENABLED=true`, private key, restart, or day-7-without-phrase cannot unlock live
- Duplicate idempotency key does not POST a second order
- Order POST timeout reconciles and fail-closes
- Unusable DB path raises `DatabaseError` (fail closed)
- Stale books, invalid Grok output (p=1.2 / malformed), and risk-limit breaches reject
- Reconciliation mismatch triggers kill switch
- CLI banners `MODE: PAPER` by default
- `request-live-activation` refuses to write an activation record before 7 full days

## Paper path

Real Gamma/CLOB data → filters → evidence → Grok structured `MarketEstimate` → edge/EV/Kelly/risk gates → `PaperBroker` walks the real book (no midpoint fills).

Paper works without `PRIVATE_KEY`. The live broker never constructs `AsyncSecureClient` unless every lock is open.

## Not claimed

- Live trading readiness
- Edge/Kelly parameter optimality
- That paper P&L will match live P&L
- That Grok probabilities are calibrated (no resolved sample yet)

Start the 7-day clock with `python -m app.cli run`.

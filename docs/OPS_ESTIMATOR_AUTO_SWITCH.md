# Ops note — estimator auto-switch (Tom)

## Enable (week-2 paper)

```bash
ESTIMATOR=microstructure
ESTIMATOR_AUTO_SWITCH=1          # default ON when unset; set 0 to disable flip-back
AI_SESSION_BUDGET_USD=10
```

Survival gates unchanged: `MIN_EDGE`, `MAX_SPREAD`, mid band, Kelly `0.25`, kill floor, weekly stop, caps.

## Behavior

1. No fills: LLM until burn ≥ $10 → DIE `ai_session_budget` → **micro** (no LLM flip-back while burn ≥ $10).
2. After fills: **LLM** when `daily_realized_pnl > burn` and `burn < $10`; **micro** when `realized < burn` or `burn ≥ $10` (or realized unknown/equal — fail closed).
3. Open tickets still exit/hold on either path.

## Log lines to expect

```
REGIME mode=… reason=… burn=… realized=… screening=… fills=…
ESTIMATOR_SWITCH from=micro to=llm provider=grok:… reason=pnl_gt_burn burn=… realized=…
ESTIMATOR_SWITCH from=llm to=micro provider=micro reason=realized_lt_burn burn=… realized=…
ESTIMATOR_SWITCH from=llm to=micro provider=micro reason=burn_exhausted burn=… realized=…
SCREEN provider=micro reason=screening_stop …
ESTIMATE provider=micro …
```

`system_events.kind=ESTIMATOR_SWITCH` carries the same `reason` / `provider` / `burn` / `realized_pnl`.

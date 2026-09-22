# Ops note — micro |I| floor (Tom)

Default `MICRO_MIN_ABS_I` is now **0.55** (was 0.40). Max strategy lock.

Unset or blank `MICRO_MIN_ABS_I` uses 0.55. Any other value still overrides. If the running `.env` still has `MICRO_MIN_ABS_I=0.40`, that line keeps the old floor — delete it or set `0.55`.

```bash
MICRO_MIN_ABS_I=0.55
```

`|I|` below 0.55 rejects new micro entries as `micro_weak_imbalance` (same reason). `|I| == 0.55` still passes this gate.

Unchanged: coin-flip mid band `[0.45, 0.55]` (`micro_coin_flip_mid`), `MICRO_LAMBDA=0.08`, `ESTIMATOR_AUTO_SWITCH` default ON. Survival gates stay: `MIN_EDGE` 0.05, `MAX_SPREAD` 0.06, mid 0.10–0.90, Kelly 0.25, kill, weekly, caps.

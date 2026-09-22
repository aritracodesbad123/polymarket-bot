# Ops note — position cap on adds (Tom)

`MAX_POSITION_USD=25` was only checked against the new order. After the 6-hour idempotency window a later BUY on the same token is an add. A small add (itself under $25) could fill on top of a ticket already at the cap. That is the Falcons ~$25.89 print: the order gate never added the open cost to the new fill.

## What the lock uses

Cost basis, per outcome token: `shares × avg_price`, plus any resting BUY on that token (`remaining × limit`).

Status / console `notional` is the **mark** (`shares × mid`). A rally can show above $25 with cost still under the cap. The lock does not sell that ticket. Compare `entry` (avg price) × shares when checking Falcons.

## Policy

| Situation | What happens |
|---|---|
| New entry | Still sized down to the stricter of `MAX_POSITION_USD` and the percentage position cap. A walked fill that would print above the cap is rejected (`position_usd_cap`), not submitted. |
| Add that fits under the cap | Allowed. Example: cost $20, buy $4 → cost $24. |
| Add that would finish above $25 | **Rejected in full.** Not clipped to the leftover room. Example: cost $20, buy $10 → `position_usd_cap` (not resized to $5). |
| Already over $25 (Falcons) | Left as-is. No flatten in this change. Any further BUY that increases that token is rejected. Sells / holding exits are unchanged. |
| Total book `MAX_TOTAL_EXPOSURE_USD=500` | Unchanged clip: the order is sized down to the remaining room. If the walked fill would still finish above $500, the order is rejected (`exposure_usd_cap`). |

Survival, Kelly `0.25`, `MIN_EDGE`, and `MAX_SPREAD` are unchanged.

## What you should see

- `TRADE_REJECTED` / decision `reject_reason=position_usd_cap` with gate detail `policy=reject_add` and `cost=… add=… cap=25`.
- Executor refuses the same way before the order row is written, including when inventory cannot be read.
- Open Falcons size does not change because of this deploy. Only the next add is blocked.

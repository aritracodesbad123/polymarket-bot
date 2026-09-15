# POLYGROK technical notes

Source of truth: current official Polymarket and xAI docs (inspected 2026-09-15).
The Vincent article and legacy `main.py` are not specifications.

## 1. Current Polymarket architecture

- Prediction markets are binary YES/NO outcome tokens on Polygon.
- Discovery metadata: Gamma API `https://gamma-api.polymarket.com`.
- Matching/order books: CLOB `https://clob.polymarket.com` (CLOB V2 since 2026-04-28).
- Realtime: `wss://ws-subscriptions-clob.polymarket.com/ws/market` (public) and `/ws/user` (L2 auth).
- Settlement collateral is **pUSD**, not USDC.e.
- Multi-outcome events may use the Neg Risk exchange (`neg_risk=true`).

## 2. Current SDK / API

Official Python SDK for new work: `polymarket-client`.

- Public: `AsyncPublicClient` / `PublicClient` — `list_markets`, `get_market`, `get_order_book`, `subscribe`.
- Trading: `AsyncSecureClient.create(private_key=..., wallet=...)` — `place_limit_order`, `place_market_order`, positions.
- CLOB-only package `py-clob-client-v2` still exists; unified SDK is the documented default.
- Legacy `py-clob-client` (V1) does not work against production CLOB V2.

Gamma listing: prefer `/markets/keyset` (limit max 100). Fallback `/markets`.

Order book: bids ascending / asks descending on the wire; **best is the last element**. POLYGROK normalizes to best-first.

## 3. Authentication

- L1: wallet EIP-712 to create/derive API credentials (`apiKey`, `secret`, `passphrase`).
- L2: HMAC-SHA256 over timestamp + method + path + body for private CLOB routes.
- Exchange EIP-712 domain version is **2**. ClobAuth domain remains version 1.
- Signature types: 0 EOA, 1 POLY_PROXY, 2 GNOSIS_SAFE, 3 POLY_1271 deposit wallet.
- POLYGROK paper mode never constructs `AsyncSecureClient` and never reads `PRIVATE_KEY`.

## 4. Collateral / assets

- pUSD: Polygon ERC-20, 6 decimals, USDC-backed (`docs.polymarket.com/concepts/pusd`).
- Trading requires pUSD + Conditional Tokens approvals on Standard and Neg Risk exchanges.
- USDC.e wrapping is an onramp concern, not CLOB order collateral.

## 5. Order types

- All CLOB orders are limits. "Market" = marketable limit (`place_market_order`).
- Lifetimes: GTC (omit expiration) or GTD (unix seconds; expires 1 minute before stated time; min ~3 minutes).
- Accept statuses: `live`, `matched`, `delayed`.
- POLYGROK prefers GTC limits. Never submits on a stale book.

## 6. Rate limits

Cloudflare IP windows (10s unless noted):

- Gamma `/markets`: 300
- CLOB general: 9,000
- `/book`: 1,500; `/books`: 500
- Separate per-signer order/cancel token buckets (Standard: 40 order tokens/s, burst 60)

## 7. Fees

Takers only, applied at match time, not signed into the order:

`fee = C × feeRate × p × (1 − p)`

Geopolitics: 0. Crypto 0.07. Sports/economics/culture/weather/other 0.05. Politics/finance/mentions/tech 0.04.

## 8. Tick size / min size / neg risk / resolution

- `tick_size` and `min_order_size` are on the book and market objects.
- Neg risk: convert NO of one outcome into YES of all others. Do not trade unnamed placeholders.
- Resolution is independent of CLOB; winning tokens redeem to $1 collateral.

## 9. Current xAI / Grok API

- Model: `grok-4.6` (500k context, knowledge cutoff 2026-02-01).
- Price (<200k prompt): $2 / $6 per 1M input/output tokens.
- Structured outputs: `xai_sdk.AsyncClient` `chat.parse(PydanticModel)`.
- Server tools: `web_search`, `x_search` ($5 / 1k calls) — ResearchProvider only.
- T0 limits: 150 RPS, 50M TPM.
- Grok has no live knowledge without search tools.

## 10. Outdated article / legacy-bot details

Do not copy:

- `py-clob-client` V1
- USDC.e as trading collateral
- Fees embedded in the signed order
- Gemini/Claude free-text JSON parsing
- Midpoint / last-mid paper fills
- `live_execution = False` as the only live lock
- Gamma offset pagination as the primary scanner
- Assuming 5% edge or quarter-Kelly is optimal

## 11. Ideas retained

Scan → cheap filter → evidence → Grok probability → edge/EV → quarter-Kelly
→ risk clamps → GTC limit → slippage check → SQLite trail → Telegram →
paper-first → prompt versioning → calibration → duplicate protection.

Thresholds are experiments. Grok never controls money.

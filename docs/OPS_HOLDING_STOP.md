# Ops note — holding stop not closing losers (Tom)

Week-2 paper ticket `4761828` (WTA Kuzmova vs Buyukakcay, BUY 38.76 @ 0.644) was still OPEN after the console marked it around 0.1¢ (about −$25). `HOLDING_STOP_PCT=0.25` should have sold near 48¢. Nothing here changes that knob, Survival, the caps, Kelly, `MIN_EDGE`, or `MAX_SPREAD`.

## What actually left it open

1. **The exit reused the entry book filter.** A stop that was already true still had to clear `filter_book` inside the executor. A one-sided 0.1¢ bid has no spread, so that check returns `spread_too_wide` and the sell never hits the book. The console mid is that same bid. The stop math was fine; the sell was refused.
2. **One failed sell blocked the rest of the 6-hour idempotency window.** The decision row was written before the sell. The next cycles saw that key and returned without an event. One `HOLDING_EXIT_FAIL`, then silence, ticket still OPEN.
3. **No bid was a silent `continue`.** Empty bid side did not log and did not retry. A rejected paper sell that did reach the broker (`no_bid_fill`) was also reported as success, because the executor returned no error when nothing filled.
4. **Price 0 was deleted, then treated as “no mark”.** Book normalize kept only `0 < price < 1`. A print at 0 became an empty book, `diagnose_holding` returned `ok` / `no_mark`, and equity ignored a 0 bid (`if best_bid`). The console still showed the level.
5. **Resolved markets were never settled.** The scanner only loads open markets. After the CLOB went empty the holding loop had no resolution price, so the row stayed a live ticket at entry.

Checked and not the cause: microstructure, the post-fill screening stop, estimator auto-switch, and regime DIE. Holding review already runs at the start of every non-HALTED cycle. A HALTED book (kill floor or weekly stop) still does not trade; that did not change. Paper sells do not consult min order size or tick size; those were not why this ticket stayed up.

## What one cycle does now

| Situation | Result |
|---|---|
| Unrealized loss at or past `HOLDING_STOP_PCT`, any bid including 0 | Sells that cycle. `HOLDING_STOP` records `mark_source` and `mark`, then `HOLDING_EXIT`. |
| Same, but no bid (or the sell is rejected) | Position stays open. `HOLDING_EXIT_FAIL` names the reason. Next cycle tries again. A previous rejected exit does not block it. |
| Gamma market closed/resolved and the outcome price is pinned near 0 or 1 | Settles at that price (`HOLDING_SETTLE`, `mark_source=resolution`). Cash is shares × resolution. The position row is dropped on the cycle snapshot. No taker fee. |
| No price and the market is not resolved | No invented fill. The stop does not fire without a mark. |

## One-time cleanup of tickets already stuck

Do **not** `DELETE` the position row and do **not** hand-edit cash. That drops the loss and leaves equity wrong. The old idempotency row does not need to be deleted either; a new cycle does not honor a rejected exit key.

1. Deploy this build and let the paper loop run **one** cycle on `polygrok-week2-5000.db`.
2. `4761828`: if Gamma has it closed, that cycle settles the loser at the outcome (0 for the YES you held) and the console ticket goes CLOSED. If it is still open and the book is past the stop, that cycle sells. If the book has no bid, you get a fresh `HOLDING_EXIT_FAIL` every cycle until a bid shows up or the market resolves — not silence.
3. Confirm in `system_events`: `HOLDING_STOP` (or `HOLDING_SETTLE`) with `mark_source` and `mark`, then `HOLDING_EXIT` or `HOLDING_SETTLE`. A ticket that is still OPEN after that cycle must have a new `HOLDING_EXIT_FAIL` from this process. If it does not, the loop is not on this build or the process is HALTED.

## CLOB 404 on a held book

A 404 means the book was removed. It is not an API outage and must not call `note_api_failure`. The cycle asks Gamma again (same settlement as above). A pinned outcome settles. A closed market that is not resolved stays open, flagged `book_gone`, with `HOLDING_BOOK_GONE` once per cycle and the last mark kept. The halt latch and the operator clear are in `docs/OPS_HALT.md`.

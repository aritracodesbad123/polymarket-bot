# Ops note — halt latch (`repeated_api_failures`)

The paper loop writes `HALTED` on `system_state` and then keeps running. Until this build, a halted cycle returned without a log line and without a `system_events` row. After the 23 Sep book-404 halt the process stayed up for days and looked dead.

Nothing here changes Survival, the kill floor, the weekly stop, the daily loss cap, Kelly, `MIN_EDGE`, or `MAX_SPREAD`.

## What tripped it

Held CLOB books for closed markets came back **404** (the book is gone). `_review_holdings` treated every exception from `get_order_book` as an API fault, including that 404. Each cycle, every dead book called `note_api_failure()`. Eight faults trip `repeated_api_failures`. Thirteen open tickets did that in one pass.

A separate live book read (SDK, no deadline) could sit forever, so the cycle never reached the next log line.

## What a halt does

`note_api_failure` counts in memory. Any successful API call zeros that counter: a Gamma scan, a Gamma market read that returns a market, or an order book (including an empty book). A 404 does not count as a fault and does not count as a success. The reset is not "N healthy cycles." It does **not** clear a halt that has already been written.

Once `halted=1`, the flag stays until an operator clears it. The counter is irrelevant after that: the next cycle does not scan, does not trade, and does not auto-resume. Survival, kill-floor, weekly equity, daily realized loss, drawdown, and operator `kill` use the same latch. This build does not auto-clear any of them.

There is no env var that clears a halt. The command is:

```bash
python -m app.cli resume-paper
```

`resume-paper` sets paper mode and clears `halt_reason`. It does not move cash, positions, the week baseline, or the day-scoped AI burn / realized-loss counters. A weekly stop or daily loss cap that is still true will halt again on the next cycle. `reset-week-baseline` does not clear a halt.

While halted, every cycle logs and writes:

```text
HALTED reason=<halt_reason> since=<system_state.updated_at>
```

`since` is the timestamp stored when the halt was written.

## 404 versus a real fault

| Book / Gamma result | API-failure counter | Position |
|---|---|---|
| CLOB 404 / not found | not counted | Gamma settlement if the outcome is pinned (`HOLDING_SETTLE`). If Gamma has no pinned resolution (closed but unresolved, or no market row), the ticket stays open, `book_gone`, last mark kept, `HOLDING_BOOK_GONE` once per position per cycle. Next cycle tries settlement again. |
| Screening 404 on the YES book | not counted | Candidate skipped. |
| 5xx, 429, timeout, network | counted | Ticket unchanged. Eight faults still halt with `repeated_api_failures`. A later successful book, Gamma market, or scan zeros the streak. |
| Gamma 404 (no such market id) | not counted | `get_market` returns nothing. The book path still runs. |

Order-book and Gamma calls use a 20s deadline. A timeout is a fault. It does not freeze the loop.

## Clear the 23 Sep halt and re-mark the open tickets

The running process is the old build. Leaving it up after a resume will 404-count again and halt immediately. Do not `DELETE` position rows and do not edit cash. That drops the loss and leaves equity wrong.

1. Stop the paper process.
2. Deploy this build. Point it at the same DB (`DB_PATH`, the week-2 file).
3. `python -m app.cli status` — expect `halted: True repeated_api_failures` and the open tickets still listed. `python -m app.cli positions` prints shares and average price. Average price is cost, not the last console mark.
4. `python -m app.cli resume-paper`.
5. Start `python -m app.cli run`. The in-memory failure counter starts at 0 in the new process.
6. Watch one cycle in the log and in `system_events`:
   - Gamma has a pinned outcome (near 0 or 1): `HOLDING_SETTLE`, ticket leaves the open book, cash is shares times that outcome. That is the safe re-mark. Do not type a price in by hand.
   - Book 404 and Gamma is not resolved: ticket stays OPEN, `HOLDING_BOOK_GONE`, `book_gone` set, last stored mark kept. On this first start there is no stored mark yet (the old process never saved one), so equity uses **average price** until a book returns or Gamma resolves. The old console mid is not in the database. Do not copy it into cash.
   - A real 5xx or timeout still increments the fault counter. Thirteen 404s must not.
7. If the log shows `HALTED reason=...` again, read the reason. `resume-paper` will not stick while a kill floor, weekly stop, or daily loss cap is still true.

`python -m app.cli open-marks` uses the same book timeout. A 404 is `kind=book_gone` and prefers the last mark saved on the portfolio snapshot over entry.

# Model-fallback stack (discussion verdict)

**Status:** implemented on the POLYGROK `app/ai` path (Grok preflight → Vertex Gemini 3.xx then 2.xx).
**Date:** 2026-09-15
**Agreed by:** Tom / John / Max (unanimous); Aritra green-lit coding.

## Context

The POLYGROK paper bot is **paused** after xAI/Grok credits were exhausted (`research_failed` / `PERMISSION_DENIED`).

- Cash/equity **$50** remains intact.
- **Survival Mode** stays in effect.
- **No silent provider swaps.**

Stay paused until **either** xAI credits are restored **or** this stack is implemented, fail-closed, and Max has reviewed the safety test — then `resume-paper`.

## Three-step fail-closed stack

1. **Grok/xAI credit preflight** — if credits are alive, stay on Grok. Never skip this just to keep trading.
2. **Only on confirmed credit/auth hard-fail** → enter the Gemini path, with a **temporary tighten** (+1–2¢ min edge **or** lower Kelly) until a paper sample of Gemini vs mid exists; **human reset after**.
3. **Intra-Gemini model cascade** (primary → secondary → …) → if every Gemini model fails → **abstain** (no trade).

Fail closed at every step. A dead Grok path does not authorize a looser Gemini trade. A dead Gemini cascade does not authorize a trade at all.

## Constraints

- Same estimate schema and fee-aware edge gates on every provider.
- Wire in `app/ai` on the **live POLYGROK path** — **not** the dead root `main.py` Gemini bot.
- No silent provider swap.
- No loosening spreads/floors to force fills.
- Ops: stay paused until xAI credits restored **or** this stack is implemented, fail-closed, and Max has reviewed the safety test — then resume-paper.

## What this is not

- Not a coding ticket until Aritra says implement.
- Not permission to swap providers in production/paper without the preflight and tighten.
- Not a change to live-unlock, kill switch, or Survival Mode.
- Not an instruction to revive or extend `main.py`.

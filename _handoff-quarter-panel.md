# Handoff addendum — the 這一季 (quarter) panel discussion

Companion to `_handoff-2026-08-15.md`. History of what Momo asked about this panel, what's
done, and what's still owed.

## What Momo said (across sessions, her framing)

1. **Chasing the ~$10k outstanding is a good quarter task** specifically because only ~18
   days remained in the quarter — a quarter objective should be something the remaining
   days can actually move. This led to the rule: pick the objective TYPE from the binding
   lens diagnosis (timing→chase, income→book, spending→trim) and SIZE it from what that
   lever actually holds. **Not yet implemented.**
2. **Savings goals shouldn't specify the source** — a saved dollar can come from spending
   less or earning more, the goal is the amount. She asked: how does money actually flow
   from income into a "to-be-allocated savings pool"? Is there a per-quarter tracker?
   This is the 季目標 pot question — `close_session` writes `cfg_season_pot`, nothing
   reads it. **Deliberately parked for the jars design (6.7):** the pool should become a
   jar instance, filled at 期末結算, not a fourth ad-hoc system.
3. **Closure and initiation happen in LINE.** When the quarter ends, the panel should not
   keep computing — it should show 「等結算」(awaiting the closure meeting) until the
   meeting happens in LINE. Same for period close: she wants 陳會計 to report earnings at
   session close and let HER decide how much moves to the quarter goal (the "income tap",
   her words, from the daily-budget design). **Neither state nor meeting exists yet —
   this is Phase 6.9.**
4. **自己轉 confused her** — it was her own rent (Zelle to mom) reading as a mystery
   category, amortised from June though rent started July. **Fixed:** renamed
   房租那類自己轉的, fixed costs have `since` dates, phantom ~$400 June rent gone.

## Current state of the panel (after 2026-08-15 batches)

- 這一季 card on 接案 = settlement (cash in/out, unpaid-with-discount, verdict) PLUS the
  newly-wired scoreboard: frozen 生存/持平/目標 targets, landed(solid)/booked(faded)
  progress tracks, 還差 per tier, dated event log, 這季自己談下來的 separated.
- Double-count fixed: booked reconciles against landed (±2%); matches skipped and
  surfaced as probably_landed (「好像已經進來了，跟阿姨說把它劃掉」).
- 本期口袋 (her other confusion) now has an ⓘ explaining it: unspent daily allowance
  accumulating within the fortnight; raises draw only from it.

## Still owed on this panel, in order

1. **等結算 state** — when `today > season end` and no closure has been run, the panel
   freezes and says 「這一季結束了，等結算會議」 instead of projecting.
2. **The closure meeting (6.9)** — scheduled LINE ritual at period/quarter end: report
   the numbers, run the income tap (she allocates surplus to the quarter goal / jars),
   write the allocation via a tool, THEN the panel unfreezes into the new quarter.
   Depends on jars for where allocated money goes.
3. **Quarter objective sizing** — type from binding lens, size from the lever's actual
   capacity (e.g. chase task sized by chaseable outstanding, book task by days × rate
   within remaining calendar). Renders as one objective line on the panel.

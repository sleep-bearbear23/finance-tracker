# 2026-08-11 — 秀琴阿姨 wired into the 機房 (control room)

*Changes made from Momo's other Claude session (the one that builds 張特助 / 好日報
Windland Daily). Written down so whoever works on this repo next isn't surprised.*

## Why

Momo now runs four LINE channels with a strict doctrine:

| channel | role |
|---|---|
| 張特助 | assistant (calendar, mail, projects) |
| 好日報 Windland Daily | one-way content feed |
| **秀琴阿姨** | money — her own voice, her own DB |
| **機房 (control room)** | internal ops: deploys, failures, tests. No persona. |

Rule: **the personas never speak in machine terms.** Deploy notices, job failures and
import chatter belong in the 機房, not in 秀琴阿姨's mouth. She keeps her channel for
what she's actually for — asking what Momo bought, budgets, reports.

## What changed in this repo

**New file `app/opsroom.py`** — one async helper, `say(text)`. Fire-and-forget HTTP POST
to the control room. Silent on any failure by design: if the control room is down it must
never affect the finance bot.

**`app/main.py`:**

1. **New `GET /health`** — machine-readable status the control room polls:
   `{ok, txn_7d, awaiting, last_txn, last_sync}`. Read-only, no secrets, wrapped in
   try/except so a bad query returns `{ok: false}` instead of a 500.
   *Note:* `txn_7d` counts by `posted_at`, **not** `created_at` — the Apple Card backfill
   stamps thousands of old rows with a recent `created_at` and reported "1756 txns/7d".

2. **`_poll_job`** — now writes `last_poll_at` / `last_poll_ok` to KV (that's what
   `/health` reads), pings the control room when new charges import, and reports sync
   failures **once per error type** (KV `poll_fail_reported`) so a broken bank link
   doesn't nag every 15 minutes.

3. **`_reconcile_job`** — pings the control room on a successful merge and on failure.

4. **`announce_deploy` — BEHAVIOUR CHANGE.** It no longer LINE-pushes Momo via
   `llm.deploy_note()`. It posts to the control room instead. **She will no longer see
   🚀 deploy messages from 秀琴阿姨 in LINE — this is intended, not a regression.**
   (`llm.deploy_note` is now unused by main.py; left in place in case it's wanted back.)

5. Added `from datetime import timedelta`, `opsroom` to the package imports.

## New env vars (Railway, finance-tracker service)

```
OPS_URL=https://web-production-2a9bd.up.railway.app   # Windland's app
OPS_KEY=<same value as Windland's LOG_KEY>
```

Both unset ⇒ `opsroom.say()` is a no-op and everything behaves as before. Nothing else
in this repo depends on them.

## The other side (for reference, lives in the momo-assistant repo)

- `POST /ops_say?key=<LOG_KEY>` with `{"from": "...", "text": "..."}` → forwards to the
  控制室 LINE channel and logs it as an `ops` event.
- The control room polls `FIN_URL/health` for its `status` card.

## Untouched

Her persona, LINE webhook, enrichment loop, budgets, reports, dashboard, SimpleFIN
ingest logic, models/schema. No DB migration.

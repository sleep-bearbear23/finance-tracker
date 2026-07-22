# 秀琴阿姨 — Finance Analyst Bot

A standalone LINE bot that watches your bank through SimpleFIN, texts you to ask what you actually bought, categorizes it, and answers questions about your money — all in the voice of a guilt-tripping Taiwanese auntie. Runs on Railway, powered by Claude. Completely separate from 張特助.

## What's built (Wave 1)

- **Bank sync** — polls SimpleFIN every 15 min, stores transactions in Postgres.
- **Apple Card real-time** — an iOS Shortcut sends each Apple Pay tap straight into LINE (a `[[TAP]]` message the bot recognizes), plus manual "spent $12 at X" logging over LINE.
- **Enrichment loop** — new charges wait a 5-min quiet window, then she sends *one* grouped LINE message asking what each was. Your free-form reply is parsed back onto each charge (note + category).
- **Auto-categorization** — a first-guess category on every charge from the merchant name; your reply corrects it.
- **Q&A** — ask her "食物這個月花多少？" and she answers in persona, from real data.
- **Silent first run** — on first boot she imports ~45 days of history *without* nagging; only charges that appear *after* that get the "what did you buy?" treatment.

## What's built (Wave 2)

- **Biweekly budget from real income** — she averages your actual deposits over the trailing 6 weeks into a fortnightly figure, subtracts your fixed costs and savings, and the rest is your discretionary allowance. Rent/utilities/subscriptions are excluded from "spending" so they're not double-counted. Right model for lumpy freelance income.
- **Savings goal** — set an amount per two weeks / per month / as a % of income; it comes off the top before your allowance is figured.
- **Reports** — weekly (Sun 6pm), monthly (1st), quarterly (Jan/Apr/Jul/Oct) — category breakdown, top merchants, budget status, all in her voice.
- **Overspend nudges** — she checks every 6h and pings you once at 80% and once at 100% of the biweekly allowance (no spam).
- **Onboarding interview** — on first contact she asks your fixed monthly costs + savings goal and stores them. Income she figures out herself from your deposits.
- **Apple Card month-end reconciliation** — when the SimpleFIN statement lands (5th of month), live-captured taps/manual logs are matched to it, the bank record is kept as truth (inheriting your note + category), and the duplicate is dropped from all totals.

## Coming next (Phase 2)

The live web dashboard — a persistent page reading this same database for running totals, charts, and budget/savings progress. Everything it needs is already in the schema.

---

## Setup (one-time)

### 1. SimpleFIN
Sign up at [bridge.simplefin.org](https://beta-bridge.simplefin.org/), link your accounts, and generate a **Setup Token** (a long base64 string). You'll paste it as `SIMPLEFIN_SETUP_TOKEN` — the bot exchanges it for a permanent access URL on first boot and stores it, so you only do this once.

### 2. LINE channel (hers, not 張特助's)
In the [LINE Developers Console](https://developers.line.biz/): create a new **Messaging API** channel. Grab the **Channel secret** and issue a **Channel access token (long-lived)**. After you deploy (step 3), set the channel's **Webhook URL** to `https://<your-railway-domain>/line/webhook` and turn **Use webhook** on. Turn **off** auto-reply messages so she's the only one talking.

### 3. Railway
1. Push this folder to a GitHub repo, then "New Project → Deploy from GitHub" on Railway.
2. Add the **Postgres** plugin (it injects `DATABASE_URL` automatically).
3. Under Variables, set everything from `.env.example` except `DATABASE_URL`:
   - `ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL` (use the same model string 張特助 uses)
   - `LINE_CHANNEL_ACCESS_TOKEN`, `LINE_CHANNEL_SECRET`
   - `SIMPLEFIN_SETUP_TOKEN`
   - `TIMEZONE=America/Los_Angeles` (optional; already the default)
4. Deploy. Railway runs the `Procfile` (`uvicorn app.main:app`).

### 4. Apple Card — the iOS Shortcut (real-time, via LINE)

SimpleFIN only gets Apple Card at month-end, so for daily Apple Card spend we skip the bank entirely: your iPhone reports each tap straight into LINE, and the bot reads it like any other message. No web endpoint or secret needed.

1. On your iPhone: **Shortcuts → Automation → + → Create Personal Automation → Transaction**.
2. Choose the card(s) to watch (Apple Card etc.), and set it to **Run Immediately**.
3. The automation gives you **Receive Transaction as input** (將交易作為輸入接收) — leave it.
4. Add a **Text** action and type this, inserting the transaction's variables where shown:
   ```
   [[TAP]] {Amount} | {Merchant} | {Card}
   ```
   (In 中文 Shortcuts those magic variables are 數量 / 商家 / 卡片或票卡.)
5. Add the **LINE → Send Message** action: send that **Text** (not the raw transaction) to **秀琴阿姨** — her LINE account, once her channel exists.
6. Save.

Every Apple Pay tap now sends a `[[TAP]] …` message to her; the bot recognizes the `[[TAP]]` marker, records the charge, and folds it into the grouped "what did you buy?" flow. The marker is how she tells an automated tap apart from something you actually typed.

**Two things to verify when you test it:** (a) whether LINE's send action fires automatically or asks you to tap send once — if it needs a tap, it's still just one tap per purchase; (b) that the message reaches the *bot's* webhook — it will, because sending to a LINE official account (the same way 張特助 already receives your messages) delivers a normal message event.

**Coverage note:** the tap trigger fires on Apple Pay taps. Online purchases or physical-card swipes won't tap — just tell her over LINE ("spent $30 at X") and she logs it. The month-end SimpleFIN statement reconciles anything missed (Wave 2).

*(There's also an optional `/ingest/tap` HTTP endpoint + `INGEST_TOKEN` in the code if you ever prefer a direct POST instead of the LINE route — but the LINE method above is simpler and recommended.)*

### 5. Say hi
Add her LINE account as a friend and send any message. She captures your user ID from that first message (so she knows where to push proactive nags), greets you, and she's live.

---

## How it behaves

- **Polling:** every `POLL_INTERVAL_MIN` minutes (default 15). Most banks only refresh a few times a day upstream, so faster polling rarely helps.
- **Nagging:** every spend charge, but grouped — she waits `DEBOUNCE_MINUTES` (default 5) of quiet so a shopping trip of 10 taps becomes one message, not ten.
- **Replies:** while charges are pending she figures out whether your message is answering them or asking a question, and routes accordingly.

Tune any of these in Railway Variables without code changes.

## Local dev / tests

```bash
pip install -r requirements.txt
cp .env.example .env   # fill in real values
uvicorn app.main:app --reload
```

The logic (debounce grouping, reply reconciliation, backfill, income split) is covered by the smoke + integration tests used during the build.

## Project layout

```
app/
  main.py        FastAPI app, LINE webhook, scheduler
  config.py      env settings + time helpers
  db.py          async engine + KV helpers
  models.py      tables
  simplefin.py   bank polling + ingest
  record.py      Apple Pay tap / manual-log charge creation
  line_client.py LINE send + signature verify
  llm.py         all Claude calls (persona, parsing, Q&A, reports)
  persona.py     秀琴阿姨's system prompt
  categories.py  taxonomy + keyword guesser
  enrichment.py  grouped prompts + reply reconciliation
  queries.py     natural-language Q&A
  prefs.py       fixed costs + savings goal
  budget.py      biweekly budget from income cash flow
  reports.py     weekly / monthly / quarterly reports
  alerts.py      overspend nudges
  reconcile.py   Apple Card month-end reconciliation
  onboarding.py  first-run interview
```

Note: the `/ingest/tap` endpoint reads its JSON fields case-insensitively, so `Amount`/`Merchant`/`Card` (as the iOS Shortcut names them) or lowercase both work — when you wire the Shortcut to the bot, only the URL changes.

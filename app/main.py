"""FastAPI app: LINE webhook + the scheduled poll/flush jobs."""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from datetime import timedelta
from uuid import uuid4

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI, Request, Response

from . import (
    alerts,
    allowance,
    cleanup,
    dashboard,
    enrichment,
    line_client,
    llm,
    memory,
    migrate,
    onboarding,
    opsroom,
    profile,
    queries,
    reconcile,
    record,
    reports,
    retag,
    seed_applecard,
    seed_history,
    simplefin,
)
from .config import now, settings
from .db import Session, engine, get_kv, init_db, set_kv

scheduler = AsyncIOScheduler(timezone=settings.TIMEZONE)


async def _poll_job():
    async with Session() as s:
        try:
            n = await simplefin.ingest(s)
            await set_kv(s, "last_poll_at", now().isoformat(timespec="minutes"))
            await set_kv(s, "last_poll_ok", "1")
            if n:
                print(f"[poll] {n} new charge(s) awaiting context")
                await opsroom.say(f"🏦 bank sync — {n} new charge(s) imported")
        except Exception as e:  # never let a bad poll kill the scheduler
            print(f"[poll] error: {e!r}")
            try:
                await set_kv(s, "last_poll_ok", "0")
                prev = await get_kv(s, "poll_fail_reported")
                if prev != type(e).__name__:  # report each NEW failure mode once
                    await set_kv(s, "poll_fail_reported", type(e).__name__)
                    await opsroom.say(f"🔴 bank sync failing — {type(e).__name__}: "
                                      f"{str(e)[:120]}")
            except Exception:
                pass


async def _flush_job():
    async with Session() as s:
        try:
            await enrichment.flush_pending(s)
        except Exception as e:
            print(f"[flush] error: {e!r}")


async def _alert_job():
    async with Session() as s:
        try:
            await alerts.check(s)
        except Exception as e:
            print(f"[alerts] error: {e!r}")


async def _weekly_job():
    async with Session() as s:
        try:
            await reports.run_report(s, "weekly")
        except Exception as e:
            print(f"[weekly] error: {e!r}")


async def _monthly_job():
    async with Session() as s:
        try:
            await reports.run_report(s, "monthly")
            if now().month in (1, 4, 7, 10):
                await reports.run_report(s, "quarterly")
        except Exception as e:
            print(f"[monthly] error: {e!r}")


async def _reconcile_job():
    async with Session() as s:
        try:
            n = await reconcile.reconcile(s)
            if n:
                print(f"[reconcile] merged {n} live charge(s) into the statement")
                await opsroom.say(f"🧾 Apple Card reconciled — {n} live charge(s) merged "
                                  f"into the statement")
        except Exception as e:
            print(f"[reconcile] error: {e!r}")
            await opsroom.say(f"🔴 reconcile failed — {type(e).__name__}: {str(e)[:120]}")


async def _snapshot_job():
    async with Session() as s:
        try:
            await dashboard.write_snapshot(s)
        except Exception as e:
            print(f"[snapshot] error: {e!r}")


async def _reminder_job():
    async with Session() as s:
        try:
            owner = await get_kv(s, enrichment.OWNER_KEY)
            if owner:
                msg = await llm.daily_reminder()
                await line_client.push(owner, msg)
                await memory.remember(s, "assistant", msg)
        except Exception as e:
            print(f"[reminder] error: {e!r}")


async def announce_deploy():
    """On a new Railway deploy, report the commit — to the 機房, not to Momo. Build talk
    isn't 秀琴阿姨's voice (Momo's channel doctrine, 2026-08-10)."""
    sha = os.environ.get("RAILWAY_GIT_COMMIT_SHA")
    if not sha:
        return  # not on Railway (local dev) — stay quiet
    async with Session() as s:
        if await get_kv(s, "notified_sha") == sha:
            return
        note = os.environ.get("RAILWAY_GIT_COMMIT_MESSAGE") or "update"
        try:
            await opsroom.say(f"🔧 秀琴阿姨 deployed · {note} (sha {sha[:7]})")
            await set_kv(s, "notified_sha", sha)
        except Exception as e:
            print(f"[deploy] error: {e!r}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    try:  # add columns to tables create_all() already built (inflow_kind, …)
        added = await migrate.run(engine)
        if added:
            print(f"[migrate] added {', '.join(added)}")
    except Exception as e:
        print(f"[migrate] error: {e!r}")
    scheduler.add_job(_poll_job, "interval", minutes=settings.POLL_INTERVAL_MIN, id="poll")
    scheduler.add_job(_flush_job, "interval", minutes=1, id="flush")
    scheduler.add_job(_alert_job, "interval", hours=6, id="alerts")
    scheduler.add_job(_weekly_job, "cron", day_of_week="sun", hour=18, id="weekly")
    scheduler.add_job(_monthly_job, "cron", day=1, hour=9, id="monthly")
    scheduler.add_job(_reconcile_job, "cron", day=5, hour=9, id="reconcile")
    scheduler.add_job(_reminder_job, "cron", hour=settings.REMINDER_HOUR, id="reminder")
    scheduler.add_job(_snapshot_job, "cron", hour=23, minute=45, id="snapshot")  # daily net-worth point
    scheduler.start()
    try:
        async with Session() as s:  # one-time Notion history import (idempotent)
            n = await seed_history.backfill(s)
            if n:
                print(f"[seed] imported {n} historical row(s) from Notion")
            m = await seed_history.reclassify_income(s)  # pull non-work money back out of income
            if m:
                print(f"[seed] reclassified {m} non-work deposit(s) out of income")
            a = await seed_applecard.backfill(s)  # Apple Card statements 2025-01 → 2026-07
            if a:
                print(f"[seed] imported {a} Apple Card transaction(s)")
            lb = await seed_applecard.apply_labels(s)  # Momo's labeling-session answers, if present
            if lb:
                print(f"[seed] applied labels to {lb} Apple Card row(s)")
            nc, nx = await cleanup.run(s)  # wider auto-labels + sweep missed card payments
            if nc or nx:
                print(f"[cleanup] categorized {nc}, swept {nx} transfer(s) out of spending")
            ne = await cleanup.ensure_accounts(s)  # Apple Card / GS Savings / Venmo always in ledger
            if ne:
                print(f"[cleanup] added {ne} known manual account(s) to the ledger")
            nd = await cleanup.dedupe_ledger(s)  # drop manual copies of bank-synced accounts
            if nd:
                print(f"[cleanup] removed {nd} duplicate ledger account(s)")
            rt = await retag.retag(s)  # old English category names -> taxonomy ids
            if rt:
                print(f"[taxonomy] {rt}")
                await opsroom.say(f"🏷️ taxonomy migration — {rt}")
            # The budget can't start on the 1st of a period, and shouldn't pretend it did.
            # First boot stamps 起算日 = today; everything before it is recorded, not judged.
            if not await allowance.start_date(s):
                d = await allowance.set_start_date(s, now().date())
                print(f"[allowance] 起算日 set to {d}")
                await opsroom.say(f"📐 budget start date set to {d} (first cadence pro-rates)")
            rf = await retag.net_refunds(s)  # refunds inherit the category they reverse
            if rf:
                print(f"[netting] {rf}")
                await opsroom.say(f"↩️ refund netting — {rf}")
    except Exception as e:
        print(f"[seed] error: {e!r}")
    try:
        async with Session() as s:  # seed today's trend point on boot so the chart isn't empty
            await dashboard.write_snapshot(s)
    except Exception as e:
        print(f"[snapshot:boot] error: {e!r}")
    await announce_deploy()  # ping Momo that this deploy is live (once per commit)
    print("秀琴阿姨 is on duty.")
    yield
    scheduler.shutdown(wait=False)


app = FastAPI(lifespan=lifespan)
app.include_router(dashboard.router)  # /dash + /api/* (token-gated)


@app.get("/")
async def health():
    return {"status": "ok", "who": "秀琴阿姨"}


@app.get("/health")
async def health_detail():
    """Machine-readable status for the 機房 control room (Windland's admin channel polls
    this so one place can answer 'is everything running'). Read-only, no secrets."""
    from sqlalchemy import func, select

    from .models import Transaction

    out: dict = {"ok": True}
    try:
        async with Session() as s:
            # by POSTED date, not created_at — a backfill import stamps thousands of old
            # rows with today's created_at and made the control room read "1756 txns/7d"
            since = now() - timedelta(days=7)
            out["txn_7d"] = int(await s.scalar(
                select(func.count(Transaction.id)).where(Transaction.posted_at >= since)) or 0)
            out["awaiting"] = int(await s.scalar(
                select(func.count(Transaction.id)).where(
                    Transaction.status.in_(("needs_context", "prompted")))) or 0)
            last = await s.scalar(select(func.max(Transaction.posted_at)))
            out["last_txn"] = last.strftime("%m-%d %H:%M") if last else "—"
            out["last_sync"] = (await get_kv(s, "last_poll_at") or "—")[:16]
    except Exception as e:  # never let the control room's ping take her down
        out = {"ok": False, "error": f"{type(e).__name__}"}
    return out


@app.post("/project_note")
async def project_note(request: Request):
    """張特助 hands over a BOOKED project with confirmed income. She raises it with Momo
    herself, in her own voice — 「你的特助剛剛跟我說…」 — not as a forwarded system line."""
    if not settings.INGEST_TOKEN or request.query_params.get("key") != os.environ.get("OPS_KEY", ""):
        return Response(status_code=403)
    d = await request.json()
    code = str(d.get("code", ""))[:40]
    title = str(d.get("title", ""))[:80]
    company = str(d.get("company", ""))[:80]
    rate = str(d.get("rate", ""))[:40]
    days = d.get("days") or 0
    async with Session() as s:
        seen = await get_kv(s, f"proj_{code}")
        if seen:
            return {"ok": True, "duplicate": True}
        await set_kv(s, f"proj_{code}", now().isoformat(timespec="minutes"))
        owner = await get_kv(s, enrichment.OWNER_KEY)
        if not owner:
            return {"ok": True, "queued": False}
        facts = "、".join(x for x in (
            f"案子代號 {code}", title, f"製作方 {company}" if company else "",
            f"報價 {rate}" if rate else "", f"預計 {days} 天" if days else "") if x)
        try:
            msg = await llm.freeform(
                "默默的特助（張特助）剛剛通知你：她接了一個新案子，之後會有收入。\n"
                f"案子資訊：{facts}\n"
                "用你自己的口氣跟默默提這件事——開頭要讓她知道是特助跟你說的"
                "（像「你的特助剛剛跟我說…」），然後問你需要知道的那一兩件事"
                "（什麼時候付、怎麼付、要不要先算進這期預算）。三句以內，別列清單。")
        except Exception:
            msg = (f"你的特助剛剛跟我說你接了 {code} 這個案子"
                   + (f"（{company}）" if company else "") + "。"
                   + (f"報價 {rate}。" if rate else "")
                   + "錢什麼時候會進來？我先幫你記著。")
        await line_client.push(owner, msg)
        await memory.remember(s, "assistant", msg)
    return {"ok": True}


@app.post("/ingest/tap")
async def ingest_tap(request: Request):
    """Called by the iOS Shortcut on every Apple Pay tap (Apple Card real-time)."""
    if not settings.INGEST_TOKEN:
        return Response(status_code=503)  # endpoint disabled until a token is set
    data = await request.json()
    if record.get_ci(data, "token") != settings.INGEST_TOKEN:
        return Response(status_code=403)
    amount = record.get_ci(data, "amount")
    if record.coerce_amount(amount) is None:
        return Response(status_code=400)
    async with Session() as s:
        await record.record_charge(
            s, amount, record.get_ci(data, "merchant"), "shortcut", record.get_ci(data, "card")
        )
    return {"ok": True}


_Q_MARKERS = ("?", "？", "嗎", "多少", "為什麼", "為何", "怎麼", "能不能", "可不可以",
              "可以嗎", "還剩", "還能", "剩多少", "how much", "why", "can i",
              # asset / budget / report asks — never a charge answer, even mid-enrichment
              "淨資產", "總資產", "淨值", "資產", "身家", "net worth", "值多少",
              "預算", "可以花", "能花", "剩多少錢", "還有多少", "報告", "結餘", "算一次", "算一下")


def _looks_like_question(text: str) -> bool:
    t = (text or "").lower()
    return any(m in t.replace(" ", "") or m in t for m in _Q_MARKERS)


# Words that hint Momo is reporting an invoice landing or a new booking (not just asking).
_INVOICE_HINTS = ("入帳", "到帳", "收到了", "付了", "付款", "進來了", "匯了", "匯款",
                  "接了", "接到", "新的案子", "新案子", "下個月有", "談成", "簽了", "款到",
                  # a booked payment can also CHANGE — amount revised, date slipped
                  "變成", "改成", "改為", "更正", "delay", "延到", "延後", "改到", "殺青", "wrap")

# Words that hint Momo is stating an account balance / card debt (lets a first account be set by chat).
_BALANCE_HINTS = ("欠", "餘額", "戶頭", "帳戶", "存款", "balance", "剩下", "現在有", "現在是",
                  "卡債", "card", "checking", "saving", "venmo", "apple", "chase")


async def _route_text(session, text: str) -> str:
    """Route an incoming LINE message to: answer pending charges / log a new expense / Q&A."""
    from sqlalchemy import select
    from . import prefs
    from .models import Transaction

    # Balance update for a manually-tracked account (card debt, cash on hand, Apple, etc.).
    # Runs when there's a number AND the message either names a known account or clearly talks
    # about a balance — so even the FIRST account (e.g. a card she doesn't know yet) can be set.
    pend = await prefs.pending_invoices(session)
    names_pending = [str(p.get("note") or "") for p in pend if p.get("note")]
    about_invoice = any(n and prefs._norm(n) in prefs._norm(text) for n in names_pending)

    if any(ch.isdigit() for ch in text) and not about_invoice:
        prof = await prefs.get_income_profile(session)
        names = [a.get("name") for a in prof.get("accounts", []) if a.get("name")]
        if names or any(k in text.lower() for k in _BALANCE_HINTS):
            upd = await llm.parse_balance_update(text, names)
            if upd.get("amount") is not None and upd.get("name"):
                res = await prefs.update_account(session, upd["name"], upd["amount"], upd.get("type"))
                return await llm.balance_ack(res["name"], res["amount"], res["type"], res["added"])

    # Invoice tracking: a pending payment landed, or a new expected payment came up.
    # Gated on invoice-ish words so ordinary chat/questions don't pay for the extra parse.
    if about_invoice or any(h in text for h in _INVOICE_HINTS):
        cmd = await llm.parse_invoice_command(text, pend)
        if cmd.get("action") == "received" and cmd.get("which"):
            hit = await prefs.mark_invoice(session, cmd["which"], "received")
            if hit:
                return await llm.invoice_ack("received", hit)
        elif cmd.get("action") == "update" and cmd.get("which"):
            hit = await prefs.update_invoice(session, cmd["which"], cmd.get("amount"),
                                             cmd.get("when"), cmd.get("note"))
            if hit:
                return await llm.invoice_ack("update", hit)
            # nothing matched — say so instead of falling through to free-text Q&A,
            # where she would happily claim to have changed a number she never touched
            return await llm.invoice_miss(cmd.get("which"), await prefs.pending_invoices(session))
        elif cmd.get("action") == "add" and cmd.get("amount"):
            item = await prefs.add_invoice(session, cmd["amount"], cmd.get("when"), cmd.get("note"))
            return await llm.invoice_ack("add", item)

    has_pending = bool((await session.execute(
        select(Transaction.id).where(Transaction.status == "prompted").limit(1)
    )).first())

    if has_pending:
        # A clear question (net worth, budget, "how much", a report…) is never a charge answer,
        # even while charges are waiting — send it straight to Q&A.
        if _looks_like_question(text):
            return await queries.answer(session, text)
        # Otherwise let the model judge: is he explaining the charges, logging a new one, or asking?
        intent = await llm.classify_intent(text, has_pending=True)
        if intent == "answer":
            handled, confirm = await enrichment.handle_reply(session, text)
            if handled and confirm:
                return confirm
        elif intent == "log":
            parsed = await llm.parse_manual_log(text)
            if parsed.get("amount"):
                t = await record.record_charge(session, parsed["amount"], parsed["merchant"], "manual")
                return await llm.manual_confirm(t)
        return await queries.answer(session, text)

    intent = await llm.classify_intent(text, has_pending=False)
    if intent == "log":
        parsed = await llm.parse_manual_log(text)
        if parsed.get("amount"):
            t = await record.record_charge(session, parsed["amount"], parsed["merchant"], "manual")
            return await llm.manual_confirm(t)
    return await queries.answer(session, text)


async def _handle_image(session, event) -> None:
    """Read a transaction screenshot, log what's new (skipping dupes), then follow up."""
    reply_token = event.get("replyToken")
    mid = event.get("message", {}).get("id")
    try:
        img, media = await line_client.get_content(mid)
        parsed = await llm.parse_screenshot(img, media)
    except Exception as e:
        print(f"[image] error: {e!r}")
        if reply_token:
            await line_client.reply(reply_token, "阿姨這張看不太清楚，重拍一張清楚一點的給我齁。")
        return

    recorded, dupes = [], 0
    for t in parsed:
        amt = record.coerce_amount(t.get("amount"))
        if amt is None:
            continue
        out_ = str(t.get("direction", "out")).lower().startswith("out")
        signed = -abs(amt) if out_ else abs(amt)
        made = await record.record_screenshot(session, t.get("date"), t.get("merchant") or "", signed)
        if made:
            recorded.append(made)
        else:
            dupes += 1

    pend = [r for r in recorded if r.status == "needs_context"]
    if pend:
        bid, stamp = str(uuid4()), now()
        for seq, r in enumerate(pend, 1):
            r.batch_id, r.batch_seq, r.prompted_at, r.status = bid, seq, stamp, "prompted"
        await session.commit()
        head = f"收到，記了 {len(recorded)} 筆" + (f"（{dupes} 筆重複跳過）" if dupes else "") + "。\n"
        out = head + await llm.enrichment_prompt(pend)
    else:
        out = await llm.screenshot_ack(len(recorded), dupes)

    if reply_token:
        await line_client.reply(reply_token, out)
    await memory.remember(session, "assistant", out)


@app.post("/line/webhook")
async def webhook(request: Request):
    body = await request.body()
    signature = request.headers.get("x-line-signature", "")
    if not line_client.verify_signature(body, signature):
        return Response(status_code=403)

    payload = await request.json()
    async with Session() as s:
        for event in payload.get("events", []):
            if event.get("type") != "message":
                continue
            mtype = event.get("message", {}).get("type")
            if mtype == "image":
                await _handle_image(s, event)
                continue
            if mtype != "text":
                continue
            user_id = (event.get("source") or {}).get("userId")
            reply_token = event.get("replyToken")
            text = event["message"]["text"].strip()

            # First contact: remember who the owner is, greet, and kick off onboarding.
            known = await get_kv(s, enrichment.OWNER_KEY)
            if user_id and not known:
                await set_kv(s, enrichment.OWNER_KEY, user_id)
                # The deploy already live when he first says hi is obvious from the greeting,
                # so mark it seen — deploy pings start from the NEXT push onward.
                sha = os.environ.get("RAILWAY_GIT_COMMIT_SHA")
                if sha:
                    await set_kv(s, "notified_sha", sha)
                await line_client.reply(reply_token, await llm.greet())
                await onboarding.start(s, user_id)
                continue

            # Auto-tap from the iOS Shortcut: a specially-marked LINE message (not conversation).
            tap = record.parse_tap_message(text)
            if tap and record.coerce_amount(tap["amount"]) is not None:
                await record.record_charge(s, tap["amount"], tap["merchant"], "shortcut", tap["card"])
                continue  # no reply — she folds it into the grouped "what did you buy?" prompt

            # Starter-pack block pasted in from the survey: store it as budgeting inputs.
            prof = profile.parse(text)
            if prof is not None:
                summary = await profile.apply(s, prof)
                ack = await llm.profile_ack(summary)
                if reply_token:
                    await line_client.reply(reply_token, ack)
                await memory.remember(s, "assistant", ack)
                continue

            await memory.remember(s, "user", text)  # log real conversation for context

            # Onboarding interview (collect fixed costs + savings goal) takes priority.
            if await onboarding.is_pending(s):
                msg, _done = await onboarding.handle(s, text)
                if reply_token:
                    await line_client.reply(reply_token, msg)
                await memory.remember(s, "assistant", msg)
                continue

            try:
                answer = await _route_text(s, text)
            except Exception as e:
                print(f"[webhook] error: {e!r}")
                answer = "阿姨這邊有點凸槌，等一下再問我一次好無？"
            if reply_token:
                await line_client.reply(reply_token, answer)
            await memory.remember(s, "assistant", answer)

    return {"ok": True}

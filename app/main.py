"""FastAPI app: LINE webhook + the scheduled poll/flush jobs."""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from datetime import timedelta
from uuid import uuid4

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI, Request, Response

from . import (
    agent,
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
    reconcile,
    record,
    reports,
    retag,
    seed_applecard,
    seed_history,
    seed_invoices,
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


async def _claims_job():
    """Pair arriving credits with the costs they repay, and flag what needs a human.

    Momo: "she need to detect some income transaction from vendors and ask about whether
    it's a refund for something, and do matching with numbers too (cuz manual
    identification could be crazy)." So the numbers go first and she is only asked where
    two outstanding claims share an amount."""
    async with Session() as s:
        try:
            from . import claims
            out = await claims.match(s)
            if out["n_settled"]:
                print(f"[claims] settled {out['n_settled']}")
                await opsroom.say(f"↩️ 退款／報帳對上 {out['n_settled']} 筆")
            if out["n_ask"]:
                # This question is FOR Momo, not for the machine room — only she knows
                # which of two identical fares a credit repays. Ask once per credit.
                owner = await get_kv(s, "owner_user_id")
                for q in out["ask"]:
                    flag = f"asked_claim:{q['credit']}"
                    if owner and not await get_kv(s, flag):
                        opts = "；".join(
                            f"{o['date'] or ''} {o['merchant']}"
                            + (f"（{o['project']}）" if o.get("project") else "")
                            for o in q["options"][:3])
                        await line_client.push(owner,
                            f"進來一筆 ${q['amount']:,.2f}（{q['desc']}），"
                            f"金額對得上不只一筆：{opts}。是還哪一筆的？跟我說商家或日期。")
                        await set_kv(s, flag, "1")
                await opsroom.say(f"❓ {out['n_ask']} 筆退款有歧義，已經去問默默了")
        except Exception as e:
            print(f"[claims] error: {e!r}")


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
            # heartbeat, not success-of-content: the alert loop once died silently for
            # weeks because a guard returned early — a stale last_run is now a visible
            # warning on the dashboard's audit strip
            await set_kv(s, "last_run:alerts", now().isoformat())
        except Exception as e:
            print(f"[alerts] error: {e!r}")


async def _weekly_job():
    async with Session() as s:
        try:
            await reports.run_report(s, "weekly")
            await set_kv(s, "last_run:weekly_report", now().isoformat())
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
async def run_maintenance() -> list[str]:
    """The eleven data-rewriting passes, run ON PURPOSE instead of on every deploy.

    They used to live in lifespan, sharing the database with a scheduler that was already
    running — 陳會計 could be asking about a charge while a pass rewrote it — and a failed
    pass printed to stdout and told nobody. Boot now reads; rewriting history is a button
    on 訓練輪 (or POST /api/maintenance), pressed by a person who wants it, with the
    results reported back instead of scrolled away. Each pass keeps its own KV flag, so
    pressing it twice still does nothing twice.
    """
    log: list[str] = []
    say = log.append
    async with Session() as s:
        n = await seed_history.backfill(s)
        if n:
            say(f"[seed] imported {n} historical row(s) from Notion")
        m = await seed_history.reclassify_income(s)  # pull non-work money back out of income
        if m:
            say(f"[seed] reclassified {m} non-work deposit(s) out of income")
        a = await seed_applecard.backfill(s)  # Apple Card statements 2025-01 → 2026-07
        if a:
            say(f"[seed] imported {a} Apple Card transaction(s)")
        lb = await seed_applecard.apply_labels(s)  # Momo's labeling-session answers, if present
        if lb:
            say(f"[seed] applied labels to {lb} Apple Card row(s)")
        nc, nx = await cleanup.run(s)  # wider auto-labels + sweep missed card payments
        if nc or nx:
            say(f"[cleanup] categorized {nc}, swept {nx} transfer(s) out of spending")
        ne = await cleanup.ensure_accounts(s)  # Apple Card / GS Savings / Venmo always in ledger
        if ne:
            say(f"[cleanup] added {ne} known manual account(s) to the ledger")
        nd = await cleanup.dedupe_ledger(s)  # drop manual copies of bank-synced accounts
        if nd:
            say(f"[cleanup] removed {nd} duplicate ledger account(s)")
        rt = await retag.retag(s)  # old English category names -> taxonomy ids
        if rt:
            say(f"[taxonomy] {rt}")
            await opsroom.say(f"🏷️ taxonomy migration — {rt}")
        rf = await retag.net_refunds(s)  # refunds inherit the category they reverse
        if rf:
            say(f"[netting] {rf}")
            await opsroom.say(f"↩️ refund netting — {rf}")
        fp = await retag.net_family_paybacks(s)  # 媽媽的回款 come off the flex bucket
        if fp:
            say(f"[family] {fp}")
            await opsroom.say(f"👩‍👧 媽媽回款 netting — {fp}")
        iv = await seed_invoices.backfill(s)  # the day rate's evidence, from her invoices
        if iv:
            say(f"[invoices] {iv}")
            await opsroom.say(f"🧾 發票匯入 — {iv}")

    return log



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
    scheduler.add_job(_claims_job, "interval", hours=4, id="claims")
    scheduler.add_job(_alert_job, "interval", hours=6, id="alerts")
    scheduler.add_job(_weekly_job, "cron", day_of_week="sun", hour=18, id="weekly")
    scheduler.add_job(_monthly_job, "cron", day=1, hour=9, id="monthly")
    scheduler.add_job(_reconcile_job, "cron", day=5, hour=9, id="reconcile")
    scheduler.add_job(_reminder_job, "cron", hour=settings.REMINDER_HOUR, id="reminder")
    scheduler.add_job(_snapshot_job, "cron", hour=23, minute=45, id="snapshot")  # daily net-worth point
    scheduler.start()
    try:
        async with Session() as s:
            # The budget can't start on the 1st of a period, and shouldn't pretend it
            # did. First boot stamps 起算日 = today — a config default, not a rewrite,
            # so it is the one line of the old seed chain that stays at boot.
            if not await allowance.start_date(s):
                d = await allowance.set_start_date(s, now().date())
                print(f"[allowance] 起算日 set to {d}")
    except Exception as e:
        print(f"[boot] error: {e!r}")
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


# The tap Shortcut is retired (decided 2026-08-15): zero rows in the entire history.
# The nightly screenshot is the documented Apple Card pipeline.


async def _route_text(session, text: str) -> str:
    """Hand the message to her, with tools.

    This used to be a ladder of keyword gates — does the message contain 入賬, does it sit
    next to an account name, does a classifier call it a log — and anything matching none
    of them fell through to a Q&A path that could talk but not write. Momo tested exactly
    that: 「我九月的 Avia 檔期確定了 9/6-9/15，大概 $2800」 matched no gate, so it was answered
    with 「幫你加一筆待收款」 and nothing was recorded.

    There is one path now. Every write — including filing the charges she asked about — is
    a tool in :mod:`app.tools`, and she speaks only after they have run.
    """
    return agent.compose(await agent.handle(session, text))


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

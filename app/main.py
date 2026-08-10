"""FastAPI app: LINE webhook + the scheduled poll/flush jobs."""
from __future__ import annotations

import os
from contextlib import asynccontextmanager

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI, Request, Response

from . import (
    alerts,
    enrichment,
    line_client,
    llm,
    memory,
    onboarding,
    queries,
    reconcile,
    record,
    reports,
    simplefin,
)
from .config import now, settings
from .db import Session, get_kv, init_db, set_kv

scheduler = AsyncIOScheduler(timezone=settings.TIMEZONE)


async def _poll_job():
    async with Session() as s:
        try:
            n = await simplefin.ingest(s)
            if n:
                print(f"[poll] {n} new charge(s) awaiting context")
        except Exception as e:  # never let a bad poll kill the scheduler
            print(f"[poll] error: {e!r}")


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
        except Exception as e:
            print(f"[reconcile] error: {e!r}")


async def announce_deploy():
    """On a new Railway deploy, tell Momo the commit message. Idempotent per commit."""
    sha = os.environ.get("RAILWAY_GIT_COMMIT_SHA")
    if not sha:
        return  # not on Railway (local dev) — stay quiet
    async with Session() as s:
        owner = await get_kv(s, enrichment.OWNER_KEY)
        if not owner or await get_kv(s, "notified_sha") == sha:
            return  # no owner yet, or already announced this commit
        note = os.environ.get("RAILWAY_GIT_COMMIT_MESSAGE") or "有更新"
        try:
            await line_client.push(owner, f"🚀 默默，阿姨更新好、又上工了：{note}")
            await set_kv(s, "notified_sha", sha)
        except Exception as e:
            print(f"[deploy] error: {e!r}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    scheduler.add_job(_poll_job, "interval", minutes=settings.POLL_INTERVAL_MIN, id="poll")
    scheduler.add_job(_flush_job, "interval", minutes=1, id="flush")
    scheduler.add_job(_alert_job, "interval", hours=6, id="alerts")
    scheduler.add_job(_weekly_job, "cron", day_of_week="sun", hour=18, id="weekly")
    scheduler.add_job(_monthly_job, "cron", day=1, hour=9, id="monthly")
    scheduler.add_job(_reconcile_job, "cron", day=5, hour=9, id="reconcile")
    scheduler.start()
    await announce_deploy()  # ping Momo that this deploy is live (once per commit)
    print("秀琴阿姨 is on duty.")
    yield
    scheduler.shutdown(wait=False)


app = FastAPI(lifespan=lifespan)


@app.get("/")
async def health():
    return {"status": "ok", "who": "秀琴阿姨"}


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


async def _route_text(session, text: str) -> str:
    """Route an incoming LINE message to: answer pending charges / log a new expense / Q&A."""
    from sqlalchemy import select
    from .models import Transaction
    has_pending = bool((await session.execute(
        select(Transaction.id).where(Transaction.status == "prompted").limit(1)
    )).first())

    intent = await llm.classify_intent(text, has_pending)

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


@app.post("/line/webhook")
async def webhook(request: Request):
    body = await request.body()
    signature = request.headers.get("x-line-signature", "")
    if not line_client.verify_signature(body, signature):
        return Response(status_code=403)

    payload = await request.json()
    async with Session() as s:
        for event in payload.get("events", []):
            if event.get("type") != "message" or event.get("message", {}).get("type") != "text":
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

"""The web dashboard: a token-gated window into 陳會計's data and her actual math.

Two faces, one page:
  • 總覽  — net worth trend, spending breakdowns, budget, pending invoices (visual).
  • 訓練輪 — how every number is computed, the full ledger, what she's learned (transparency).

Everything is read-only. Auth is a single shared secret (DASHBOARD_TOKEN); with it unset
the whole thing 503s so it can never be left open by accident.
"""
from __future__ import annotations

import hmac
from collections import defaultdict
from datetime import timedelta
from pathlib import Path

from fastapi import APIRouter, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import select

from . import budget, networth, prefs
from .config import aware, now, settings
from .db import Session
from .models import Account, MerchantMemory, Message, Snapshot, Transaction

router = APIRouter()
_HTML = (Path(__file__).parent / "dashboard.html")


def _authorized(request: Request) -> bool:
    tok = settings.DASHBOARD_TOKEN
    if not tok:
        return False
    given = (
        request.query_params.get("t")
        or request.cookies.get("dash")
        or request.headers.get("x-dash-token")
        or ""
    )
    return bool(given) and hmac.compare_digest(given, tok)


def _deny():
    return JSONResponse({"error": "unauthorized"}, status_code=403)


# ── the trend snapshot ───────────────────────────────────────────────
async def write_snapshot(session) -> None:
    """Upsert today's point so the net-worth / budget trend keeps growing."""
    nw = await networth.compute(session)
    try:
        b = await budget.status(session)
    except Exception:
        b = {"allowance": 0.0, "spent": 0.0, "income_biweekly": 0.0}
    day = now().date().isoformat()
    row = (await session.execute(select(Snapshot).where(Snapshot.day == day))).scalar_one_or_none()
    if row is None:
        row = Snapshot(day=day)
        session.add(row)
    row.net_worth = nw["net"]
    row.assets = nw["assets"]
    row.debts = nw["debts"]
    row.cash = nw["assets"]
    row.allowance = b.get("allowance", 0.0)
    row.spent = b.get("spent", 0.0)
    row.income_biweekly = b.get("income_biweekly", 0.0)
    await session.commit()


# ── helpers ──────────────────────────────────────────────────────────
def _spend_ok(t) -> bool:
    return budget.is_spend(t)


async def _pending_block(session, nw):
    pend = await prefs.pending_invoices(session)
    items, total = [], 0.0
    today_ym = now().strftime("%Y-%m")
    for p in pend:
        amt = float(p.get("amount") or 0)
        total += amt
        items.append({
            "note": p.get("note") or "某案",
            "amount": amt,
            "when": p.get("when"),
            "overdue": bool(p.get("when") and str(p.get("when"))[:7] < today_ym),
        })
    cash_now = nw["assets"] - nw["debts"]
    return {"items": items, "total": round(total, 2), "projected": round(cash_now + total, 2)}


# ── routes ───────────────────────────────────────────────────────────
@router.get("/dash", response_class=HTMLResponse)
async def dash_page(request: Request):
    if not settings.DASHBOARD_TOKEN:
        return Response("dashboard disabled", status_code=503)
    if not _authorized(request):
        return Response("unauthorized", status_code=403)
    html = _HTML.read_text(encoding="utf-8")
    resp = HTMLResponse(html)
    # If they arrived with ?t=…, stash it in a cookie so the token leaves the URL bar.
    if request.query_params.get("t"):
        resp.set_cookie("dash", settings.DASHBOARD_TOKEN, httponly=True,
                        samesite="lax", secure=True, max_age=60 * 60 * 24 * 30)
    return resp


@router.get("/api/overview")
async def api_overview(request: Request):
    if not _authorized(request):
        return _deny()
    async with Session() as s:
        nw = await networth.compute(s)
        pending = await _pending_block(s, nw)
        try:
            b = await budget.status(s)
        except Exception:
            b = None
        accts = (await s.execute(select(Account))).scalars().all()
        return {
            "as_of": now().isoformat(),
            "net_worth": nw["net"],
            "assets": nw["assets"],
            "debts": nw["debts"],
            "accounts": nw["rows"],
            "synced": [{"name": a.name, "balance": a.balance} for a in accts],
            "pending": pending,
            "budget": b,
        }


@router.get("/api/trends")
async def api_trends(request: Request):
    if not _authorized(request):
        return _deny()
    async with Session() as s:
        snaps = (await s.execute(select(Snapshot).order_by(Snapshot.day))).scalars().all()
        net_series = [{"day": r.day, "net": round(r.net_worth, 2),
                       "assets": round(r.assets, 2), "debts": round(r.debts, 2)} for r in snaps]

        txns = (await s.execute(select(Transaction))).scalars().all()
        since_cat = now() - timedelta(days=90)
        cat = defaultdict(float)
        monthly_in = defaultdict(float)
        monthly_out = defaultdict(float)
        for t in txns:
            d = aware(t.posted_at or t.created_at)
            if not d:
                continue
            ym = d.strftime("%Y-%m")
            if _spend_ok(t):
                monthly_out[ym] += abs(t.amount)
                if d >= since_cat:
                    cat[t.category or "未分類"] += abs(t.amount)
            elif t.amount > 0 and t.status == "income":
                monthly_in[ym] += t.amount

        cat_sorted = sorted(cat.items(), key=lambda x: -x[1])
        category_spend = [{"category": c, "amount": round(a, 2)} for c, a in cat_sorted]
        months = sorted(set(monthly_in) | set(monthly_out))[-6:]
        monthly = [{"month": m, "income": round(monthly_in.get(m, 0.0), 2),
                    "spend": round(monthly_out.get(m, 0.0), 2)} for m in months]
        return {"net_worth_series": net_series, "category_spend": category_spend, "monthly": monthly}


@router.get("/api/income")
async def api_income(request: Request):
    if not _authorized(request):
        return _deny()
    async with Session() as s:
        rows = (await s.execute(
            select(Transaction).where(Transaction.amount > 0, Transaction.status == "income")
        )).scalars().all()
        rows = sorted(rows, key=lambda t: aware(t.posted_at or t.created_at) or now(), reverse=True)
        received, total = [], 0.0
        for t in rows:
            d = aware(t.posted_at or t.created_at)
            amt = abs(t.amount)
            total += amt
            method, note = "", (t.note or "")
            if "｜" in note:
                method, note = note.split("｜", 1)
            received.append({
                "date": d.strftime("%Y-%m-%d") if d else "",
                "name": t.merchant_desc, "method": method, "note": note, "amount": round(amt, 2),
            })
        pend = await prefs.pending_invoices(s)
        today_ym = now().strftime("%Y-%m")
        expected = [{
            "note": p.get("note") or "某案", "amount": float(p.get("amount") or 0),
            "when": p.get("when"),
            "overdue": bool(p.get("when") and str(p.get("when"))[:7] < today_ym),
        } for p in pend]
        return {
            "received": received,
            "received_total": round(total, 2),
            "expected": expected,
            "expected_total": round(sum(e["amount"] for e in expected), 2),
        }


@router.get("/api/brain")
async def api_brain(request: Request):
    if not _authorized(request):
        return _deny()
    async with Session() as s:
        nw = await networth.compute(s)
        prof = await prefs.get_income_profile(s)
        pr = await prefs.get_prefs(s)
        try:
            b = await budget.status(s)
            expected = await budget.expected_income_biweekly(s)
            actual = await budget.income_basis_biweekly(s)
        except Exception:
            b, expected, actual = None, 0.0, 0.0

        pend = await prefs.pending_invoices(s)
        booked = [{"note": p.get("note"), "amount": float(p.get("amount") or 0),
                   "when": p.get("when")} for p in pend]

        budget_detail = None
        if b:
            budget_detail = {
                "monthly_baseline": prof["monthly_baseline"],
                "booked_pipeline": booked,
                "expected_biweekly": round(expected, 2),
                "actual_biweekly": round(actual, 2),
                "blend": b.get("income_blend"),
                "income_biweekly": b["income_biweekly"],
                "fixed_monthly": pr["fixed_monthly"],
                "fixed_biweekly": b["fixed_biweekly"],
                "savings_amount": pr["savings_amount"],
                "savings_cadence": pr["savings_cadence"],
                "savings_biweekly": b["savings_biweekly"],
                "allowance": b["allowance"],
                "spent": b["spent"],
                "remaining": b["remaining"],
                "period_start": str(b["period_start"]),
                "period_end": str(b["period_end"]),
                "days_left": b["days_left"],
            }

        mem = (await s.execute(select(MerchantMemory))).scalars().all()
        memory = [{
            "key": m.key, "category": m.category, "note": m.note,
            "is_income": m.is_income, "necessary": m.necessary,
            "updated_at": aware(m.updated_at).isoformat() if m.updated_at else None,
        } for m in mem]

        txns = (await s.execute(select(Transaction))).scalars().all()
        def keyfn(t):
            d = aware(t.posted_at or t.created_at)
            return d or now()
        txns = sorted(txns, key=keyfn, reverse=True)[:400]
        ledger = [{
            "date": (aware(t.posted_at or t.created_at).strftime("%Y-%m-%d")
                     if (t.posted_at or t.created_at) else ""),
            "merchant": t.merchant_desc, "amount": round(t.amount, 2),
            "category": t.category, "status": t.status, "source": t.source,
            "note": t.note,
        } for t in txns]

        msgs = (await s.execute(
            select(Message).order_by(Message.created_at.desc(), Message.id.desc()).limit(120)
        )).scalars().all()
        chat = [{
            "role": m.role, "content": m.content,
            "at": aware(m.created_at).isoformat() if m.created_at else None,
        } for m in reversed(msgs)]

        return {
            "networth": nw,
            "budget_detail": budget_detail,
            "config": {
                "fixed_monthly": pr["fixed_monthly"],
                "savings_amount": pr["savings_amount"],
                "savings_cadence": pr["savings_cadence"],
                "monthly_baseline": prof["monthly_baseline"],
                "ytd_income": prof["ytd_income"],
                "cash_on_hand": prof["cash_on_hand"],
                "emergency_target": prof["emergency_target"],
                "total_debt": prof["total_debt"],
                "accounts": prof["accounts"],
            },
            "merchant_memory": memory,
            "ledger": ledger,
            "chat": chat,
        }

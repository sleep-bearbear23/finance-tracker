"""Biweekly budget built from real income cash flow — the right model for freelance income."""
from __future__ import annotations

import calendar
from datetime import date, timedelta

from sqlalchemy import select

from .config import aware, now
from .db import get_kv, set_kv
from .models import Transaction
from .prefs import get_income_profile, get_prefs

# Rent/utilities/subscriptions are covered by the user's stated fixed costs, so they're
# excluded from discretionary spend to avoid double-counting.
FIXED_CATEGORIES = {"Rent & Utilities", "Subscriptions"}
NON_SPEND_CATEGORIES = {"Income", "Transfers/Ignore"}
STATUS_EXCLUDE = {"reconciled", "ignored"}  # merged duplicates / user-ignored

PERIOD_DAYS = 14
INCOME_WINDOW_DAYS = 42  # trailing 6 weeks → 3 biweekly periods
EXPECT_HORIZON_DAYS = 56  # look 8 weeks ahead to smooth lumpy freelance income
DEFAULT_BLEND = 0.5  # how much the budget leans on actual deposits vs the plan


def biweekly_from_monthly(monthly: float) -> float:
    return monthly * 12.0 / 26.0  # 26 biweekly periods per year


def savings_biweekly(amount: float, cadence: str, income_bw: float) -> float:
    if cadence == "monthly":
        return biweekly_from_monthly(amount)
    if cadence == "percent":
        return income_bw * (amount / 100.0)
    return amount  # already biweekly


def is_spend(t) -> bool:
    return (
        t.amount < 0
        and t.status not in STATUS_EXCLUDE
        and (t.category or "") not in NON_SPEND_CATEGORIES
    )


def is_discretionary(t) -> bool:
    return is_spend(t) and (t.category or "") not in FIXED_CATEGORIES


def period_for(anchor: date, today: date) -> tuple[date, date]:
    idx = (today - anchor).days // PERIOD_DAYS
    start = anchor + timedelta(days=PERIOD_DAYS * idx)
    return start, start + timedelta(days=PERIOD_DAYS)


async def _anchor(session) -> date:
    a = await get_kv(session, "cfg_anchor")
    if a:
        return date.fromisoformat(a)
    d = now().date()
    await set_kv(session, "cfg_anchor", d.isoformat())
    return d


async def income_basis_biweekly(session, days: int = INCOME_WINDOW_DAYS) -> float:
    """Actual performance: real confirmed income over the trailing window, per biweekly period.
    Only confirmed income counts — not paybacks, transfers, or card payments."""
    since = now() - timedelta(days=days)
    rows = (await session.execute(
        select(Transaction).where(
            Transaction.amount > 0,
            Transaction.status == "income",
            Transaction.source != "notion",  # imported history is for display, not the live basis
        )
    )).scalars().all()
    total = 0.0
    for t in rows:
        d = aware(t.posted_at or t.created_at)
        if d and d >= since:
            total += t.amount
    return total / (days / PERIOD_DAYS) if days else 0.0


async def expected_income_biweekly(session, horizon_days: int = EXPECT_HORIZON_DAYS) -> float:
    """The plan: expected income per biweekly period, from Momo's stated baseline + booked gigs.

    Each calendar month in the horizon is worth max(baseline, gigs booked to land that month);
    that's spread evenly across the month's days and averaged over the horizon, so a lump payment
    lifts the budget smoothly ahead of when it lands instead of spiking the block it arrives."""
    p = await get_income_profile(session)
    baseline = p["monthly_baseline"]
    booked: dict[str, float] = {}
    for u in p["upcoming"]:
        if (u.get("status") or "pending") == "received":
            continue  # already landed as a real deposit — don't count it as still-expected
        w = u.get("when")
        try:
            amt = float(u.get("amount") or 0)
        except (TypeError, ValueError):
            amt = 0.0
        if w and amt > 0:
            booked[str(w)[:7]] = booked.get(str(w)[:7], 0.0) + amt
    if baseline <= 0 and not booked:
        return 0.0

    today = now().date()
    end = today + timedelta(days=horizon_days)
    total = 0.0
    cur = today
    while cur < end:
        dim = calendar.monthrange(cur.year, cur.month)[1]
        ym = f"{cur.year:04d}-{cur.month:02d}"
        month_income = max(baseline, booked.get(ym, 0.0)) if baseline > 0 else booked.get(ym, 0.0)
        if cur.month == 12:
            nxt = date(cur.year + 1, 1, 1)
        else:
            nxt = date(cur.year, cur.month + 1, 1)
        seg_end = min(nxt, end)
        total += (month_income / dim) * (seg_end - cur).days
        cur = seg_end
    return total * PERIOD_DAYS / horizon_days


async def budgeting_income_biweekly(session) -> dict:
    """Blend the plan (expected) with actual performance, block by block.

    - both known  → weighted blend (cfg_income_blend, default 0.5)
    - only a plan → use the plan (fresh, no deposits classified yet)
    - only actuals→ use actuals (no starter pack filled in yet — old behavior)
    """
    expected = await expected_income_biweekly(session)
    actual = await income_basis_biweekly(session)
    w = float(await get_kv(session, "cfg_income_blend") or DEFAULT_BLEND)
    w = min(1.0, max(0.0, w))
    if expected > 0 and actual > 0:
        used = (1 - w) * expected + w * actual
    elif expected > 0:
        used = expected
    else:
        used = actual
    return {"expected": expected, "actual": actual, "blend": w, "used": used}


async def status(session) -> dict:
    prefs = await get_prefs(session)
    inc = await budgeting_income_biweekly(session)
    income_bw = inc["used"]
    fixed_bw = biweekly_from_monthly(prefs["fixed_monthly"])
    sav_bw = savings_biweekly(prefs["savings_amount"], prefs["savings_cadence"], income_bw)
    allowance = max(0.0, income_bw - fixed_bw - sav_bw)

    anchor = await _anchor(session)
    today = now().date()
    start, end = period_for(anchor, today)

    rows = (await session.execute(select(Transaction).where(Transaction.amount < 0))).scalars().all()
    spent = 0.0
    for t in rows:
        d = aware(t.posted_at or t.created_at)
        if d and start <= d.date() < end and is_discretionary(t):
            spent += abs(t.amount)

    allowance = round(allowance, 2)
    spent = round(spent, 2)
    return {
        "period_start": start,
        "period_end": end,
        "income_biweekly": round(income_bw, 2),
        "income_expected": round(inc["expected"], 2),
        "income_actual": round(inc["actual"], 2),
        "income_blend": inc["blend"],
        "fixed_biweekly": round(fixed_bw, 2),
        "savings_biweekly": round(sav_bw, 2),
        "allowance": allowance,
        "spent": spent,
        "remaining": round(allowance - spent, 2),
        "days_left": max(0, (end - today).days),
        "pct_used": round(100 * spent / allowance, 1) if allowance > 0 else None,
    }

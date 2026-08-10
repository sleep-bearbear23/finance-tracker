"""Biweekly budget built from real income cash flow — the right model for freelance income."""
from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import select

from .config import aware, now
from .db import get_kv, set_kv
from .models import Transaction
from .prefs import get_prefs

# Rent/utilities/subscriptions are covered by the user's stated fixed costs, so they're
# excluded from discretionary spend to avoid double-counting.
FIXED_CATEGORIES = {"Rent & Utilities", "Subscriptions"}
NON_SPEND_CATEGORIES = {"Income", "Transfers/Ignore"}
STATUS_EXCLUDE = {"reconciled", "ignored"}  # merged duplicates / user-ignored

PERIOD_DAYS = 14
INCOME_WINDOW_DAYS = 42  # trailing 6 weeks → 3 biweekly periods


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
    # Only confirmed income counts — not paybacks, transfers, or card payments.
    since = now() - timedelta(days=days)
    rows = (await session.execute(
        select(Transaction).where(Transaction.amount > 0, Transaction.status == "income")
    )).scalars().all()
    total = 0.0
    for t in rows:
        d = aware(t.posted_at or t.created_at)
        if d and d >= since:
            total += t.amount
    return total / (days / PERIOD_DAYS) if days else 0.0


async def status(session) -> dict:
    prefs = await get_prefs(session)
    income_bw = await income_basis_biweekly(session)
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
        "fixed_biweekly": round(fixed_bw, 2),
        "savings_biweekly": round(sav_bw, 2),
        "allowance": allowance,
        "spent": spent,
        "remaining": round(allowance - spent, 2),
        "days_left": max(0, (end - today).days),
        "pct_used": round(100 * spent / allowance, 1) if allowance > 0 else None,
    }

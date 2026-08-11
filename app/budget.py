"""Half-month budget — Momo's cadence is 每月 1–15 / 16–月底, never a rolling 14 days.

Monthly commitments are split by real day count, so the 15-day half is charged 15/31 of
the rent and the 16-day half is charged 16/31. Income is a blend of the plan (stated
baseline + booked gigs) and reality (recent confirmed deposits), because freelance money
is lumpy but the plan shouldn't be fiction.
"""
from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import select

from . import period as P
from . import taxonomy as T
from .config import aware, now
from .db import get_kv
from .models import Transaction
from .prefs import get_income_profile, get_prefs

# Which categories mean what is now a property of the taxonomy, not a hard-coded list
# here — add a category and the budget follows automatically.
FIXED_CATEGORIES = set(T.of_treatment(T.FIXED))
NON_SPEND_CATEGORIES = set(T.of_treatment(T.SKIP))
STATUS_EXCLUDE = {"reconciled", "ignored"}  # merged duplicates / user-ignored

ACTUAL_WINDOW = 6      # trailing half-months used for "what really landed" (= 3 months)
EXPECT_HORIZON = 4     # half-months looked ahead to smooth lumpy bookings (= 2 months)
DEFAULT_BLEND = 0.5    # how much the budget leans on actuals vs the plan


def is_spend(t) -> bool:
    """Rows that belong in a spending bucket — including the money that came *back*.

    A refund or a production reimbursement carries the category of the charge it
    reverses and a positive amount, so summing the bucket nets it automatically.
    Momo returns 41% of what she buys on Amazon; counting only the outgoing side
    overstated her spending by hundreds of dollars a month."""
    if t.status in STATUS_EXCLUDE or (t.category or "") in NON_SPEND_CATEGORIES:
        return False
    if t.amount < 0:
        return True
    return getattr(t, "inflow_kind", None) in T.CANCELS_SPEND


def spend_amount(t) -> float:
    """Positive for money out, negative for money that came back."""
    return -t.amount


def is_discretionary(t) -> bool:
    """Does it eat the half-month allowance? Only the decisions Momo actually makes —
    固定 doesn't, 工作 doesn't (that's a business cost), 不規則 doesn't (that's a shock)."""
    return is_spend(t) and T.in_allowance(t.category)


def is_income(t) -> bool:
    """Only real pay. A friend's Zelle, a refund and a劇組報帳 are all money in, and
    none of them are earnings."""
    if t.amount <= 0:
        return False
    kind = getattr(t, "inflow_kind", None)
    if kind is not None:
        return kind == T.PAY
    return t.status == "income"  # rows from before inflow kinds existed


def eff_date(t) -> date | None:
    """Which period does this row belong to?

    Almost always the day it posted. The exception is a refund: Momo's Amazon returns
    land one to two months after the order, so counting the credit on the day it
    arrived made March look like a blowout and June look free — June 2026 came out at
    *minus* $132 of discretionary spend, which is not a thing that can happen. A matched
    refund is booked back onto the month of the charge it reverses."""
    d = aware(getattr(t, "effective_at", None) or t.posted_at or t.created_at)
    return d.date() if d else None


def split_monthly(monthly: float, key: str) -> float:
    """A monthly amount charged to one half-month, weighted by day count."""
    return P.split_monthly(monthly, key)


def savings_for(amount: float, cadence: str, key: str, income_period: float) -> float:
    if cadence == "monthly":
        return split_monthly(amount, key)
    if cadence == "percent":
        return income_period * (amount / 100.0)
    return float(amount)  # already stated per period


def current_key() -> str:
    return P.key_for(now().date())


# ── income ───────────────────────────────────────────────────────────
async def income_actual(session, end_key: str | None = None, n: int = ACTUAL_WINDOW) -> float:
    """Average confirmed income per half-month over the trailing window.
    Imported history (source='notion') is display-only and never sets the live basis."""
    end_key = end_key or current_key()
    keys = P.last_n(end_key, n)
    lo, _ = P.key_bounds(keys[0])
    _, hi = P.key_bounds(keys[-1])
    rows = (await session.execute(select(Transaction).where(
        Transaction.amount > 0, Transaction.status == "income", Transaction.source != "notion"
    ))).scalars().all()
    total = sum(t.amount for t in rows if (d := eff_date(t)) and lo <= d <= hi)
    return total / n if n else 0.0


async def income_expected(session, start_key: str | None = None, n: int = EXPECT_HORIZON) -> float:
    """Average expected income per half-month: each month is worth max(baseline, booked),
    spread across its two halves by day count, averaged over the horizon."""
    start_key = start_key or current_key()
    p = await get_income_profile(session)
    baseline = p["monthly_baseline"]
    booked: dict[str, float] = {}
    for u in p["upcoming"]:
        if (u.get("status") or "pending") == "received":
            continue  # already landed as a real deposit — not still-expected
        w, amt = u.get("when"), u.get("amount")
        try:
            amt = float(amt or 0)
        except (TypeError, ValueError):
            amt = 0.0
        if w and amt > 0:
            booked[str(w)[:7]] = booked.get(str(w)[:7], 0.0) + amt
    if baseline <= 0 and not booked:
        return 0.0

    total = 0.0
    for key in P.horizon(P.key_bounds(start_key)[0], n):
        ym = key[:7]
        month_income = max(baseline, booked.get(ym, 0.0)) if baseline > 0 else booked.get(ym, 0.0)
        total += month_income * P.month_fraction(key)
    return total / n if n else 0.0


async def income_basis(session, key: str | None = None) -> dict:
    """Blend the plan with reality. Falls back cleanly when only one side exists."""
    key = key or current_key()
    expected = await income_expected(session, key)
    actual = await income_actual(session, key)
    w = float(await get_kv(session, "cfg_income_blend") or DEFAULT_BLEND)
    w = min(1.0, max(0.0, w))
    if expected > 0 and actual > 0:
        used = (1 - w) * expected + w * actual
    elif expected > 0:
        used = expected
    else:
        used = actual
    return {"expected": expected, "actual": actual, "blend": w, "used": used}


# ── per-period flows (charts, account pages) ─────────────────────────
async def flows(session, keys: list[str], account_filter=None) -> dict[str, dict]:
    """Income / spend totals for each half-month key. account_filter(t) -> bool to scope."""
    lo, _ = P.key_bounds(keys[0])
    _, hi = P.key_bounds(keys[-1])
    rows = (await session.execute(select(Transaction))).scalars().all()
    out = {k: {"key": k, "label": P.label(k), "month_start": P.is_month_start(k),
               "income": 0.0, "spend": 0.0} for k in keys}
    for t in rows:
        d = eff_date(t)
        if not d or d < lo or d > hi:
            continue
        if account_filter and not account_filter(t):
            continue
        k = P.key_for(d)
        if k not in out:
            continue
        if is_spend(t):
            out[k]["spend"] += spend_amount(t)
        elif is_income(t):
            out[k]["income"] += t.amount
    for v in out.values():
        v["income"] = round(v["income"], 2)
        v["spend"] = round(v["spend"], 2)
    return out


# ── the headline number ──────────────────────────────────────────────
async def status(session, key: str | None = None) -> dict:
    key = key or current_key()
    start, end = P.key_bounds(key)
    prefs_ = await get_prefs(session)
    inc = await income_basis(session, key)

    income_p = inc["used"]
    fixed_p = split_monthly(prefs_["fixed_monthly"], key)
    sav_p = savings_for(prefs_["savings_amount"], prefs_["savings_cadence"], key, income_p)
    allowance = max(0.0, income_p - fixed_p - sav_p)

    # Not filtered to amount < 0: a refund inside the period has to come back off the
    # total, otherwise a returned $200 order eats the allowance twice.
    rows = (await session.execute(select(Transaction))).scalars().all()
    spent = sum(spend_amount(t) for t in rows
                if (d := eff_date(t)) and start <= d <= end and is_discretionary(t))

    today = now().date()
    allowance, spent = round(allowance, 2), round(spent, 2)
    left = P.days_left(key, today) if start <= today <= end else 0
    return {
        "period_key": key, "period_label": P.label(key),
        "period_start": start, "period_end": end,
        "days_in_period": P.days_in(key), "days_left": left,
        "days_elapsed": P.elapsed_days(key, today),
        "income_period": round(income_p, 2),
        "income_expected": round(inc["expected"], 2),
        "income_actual": round(inc["actual"], 2),
        "income_blend": inc["blend"],
        "fixed_period": round(fixed_p, 2),
        "fixed_monthly": prefs_["fixed_monthly"],
        "savings_period": round(sav_p, 2),
        "allowance": allowance,
        "spent": spent,
        "remaining": round(allowance - spent, 2),
        "per_day_left": round(max(0.0, allowance - spent) / left, 2) if left else None,
        "pct_used": round(100 * spent / allowance, 1) if allowance > 0 else None,
    }

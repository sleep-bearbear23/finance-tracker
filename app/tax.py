"""Tax reserve — money that is in Momo's account and is not Momo's money.

She's 1099. Self-employment tax is 15.3% on 92.35% of net earnings, federal income tax
sits on top, and California takes its own cut — so roughly 25–30% of net, and nothing in
19 months of card data suggested any of it was being set aside.

The reserve is a *wall-off*, not a transfer. The cash stays in Chase; this module just
subtracts it before the allowance is allowed to look at the balance, the same way the
emergency fund is subtracted. For lumpy freelance income that beats a separate account,
because the alternative is shuffling money back and forth every time a gig pays.

Nothing here is tax advice and the rate is a placeholder until a CPA who works with film
freelancers confirms it. What the code guarantees is that the money is not counted as
spendable twice.
"""
from __future__ import annotations

from datetime import date

from sqlalchemy import select

from . import budget
from . import taxonomy as T
from .config import now
from .db import get_kv, set_kv
from .models import Transaction

#: fraction of gross work income held back. Conservative on purpose — over-reserving is
#: released in April, under-reserving is a bill that can't be negotiated.
DEFAULT_RATE = 0.30
RATE_KEY = "cfg_tax_rate"
START_KEY = "cfg_tax_year_start"

# Federal is four equal quarters. California is NOT: it runs 30/40/0/30, so September has
# no state payment at all and June is the big one. Getting this wrong in either direction
# is a penalty, so the split is spelled out rather than assumed.
DEADLINES: list[dict] = [
    {"due": "2026-09-15", "covers": ("2026-06-01", "2026-08-31"),
     "federal": True, "ca_pct": 0.0, "label": "Q3"},
    {"due": "2027-01-15", "covers": ("2026-09-01", "2026-12-31"),
     "federal": True, "ca_pct": 0.30, "label": "Q4（2026 年度最後一期）"},
    {"due": "2027-04-15", "covers": ("2027-01-01", "2027-03-31"),
     "federal": True, "ca_pct": 0.30, "label": "Q1（同時是報稅日）"},
    {"due": "2027-06-15", "covers": ("2027-04-01", "2027-05-31"),
     "federal": True, "ca_pct": 0.40, "label": "Q2（加州這期最大）"},
    {"due": "2027-09-15", "covers": ("2027-06-01", "2027-08-31"),
     "federal": True, "ca_pct": 0.0, "label": "Q3"},
]

#: descriptors that mean a tax payment already went out
PAID_CATEGORY = "tax"


def _is_tax_payment(t) -> bool:
    """Recognise a payment that already went to the IRS or the FTB.

    Matching on the stored category alone missed Momo's real $1,836 IRS payment from
    2026-06-15, because that row had never been categorised — so the reserve asked her to
    hold money she had already handed over. The description is checked too."""
    return t.category == PAID_CATEGORY or T.guess(t.merchant_desc) == PAID_CATEGORY


async def rate(session) -> float:
    try:
        r = float(await get_kv(session, RATE_KEY) or DEFAULT_RATE)
    except (TypeError, ValueError):
        r = DEFAULT_RATE
    return min(0.6, max(0.0, r))


async def set_rate(session, r: float) -> float:
    r = min(0.6, max(0.0, float(r)))
    await set_kv(session, RATE_KEY, str(r))
    return r


async def year_start(session) -> date:
    raw = await get_kv(session, START_KEY)
    if raw:
        try:
            return date.fromisoformat(raw[:10])
        except ValueError:
            pass
    return date(now().year, 1, 1)


async def status(session, today: date | None = None) -> dict:
    """What's owed, what's held, what's already been paid, and what's next.

    Income is counted from real deposits only (:func:`budget.is_income`), so a friend's
    Zelle or a production reimbursement can't inflate the tax bill."""
    today = today or now().date()
    r = await rate(session)
    start = await year_start(session)

    rows = (await session.execute(select(Transaction))).scalars().all()
    earned = 0.0
    paid = 0.0
    for t in rows:
        d = budget.eff_date(t)
        if not d or d < start or d > today:
            continue
        if budget.is_income(t):
            earned += t.amount
        elif t.amount < 0 and _is_tax_payment(t):
            paid += -t.amount

    should_hold = round(earned * r, 2)
    outstanding = round(max(0.0, should_hold - paid), 2)

    nxt = next_deadline(today)
    due_now = 0.0
    if nxt:
        lo, hi = (date.fromisoformat(nxt["covers"][0]), date.fromisoformat(nxt["covers"][1]))
        window = sum(t.amount for t in rows
                     if budget.is_income(t) and (d := budget.eff_date(t)) and lo <= d <= hi)
        due_now = round(window * r, 2)

    return {
        "rate": r,
        "year_start": start.isoformat(),
        "earned_ytd": round(earned, 2),
        "should_hold": should_hold,
        "already_paid": round(paid, 2),
        "outstanding": outstanding,      # <- subtracted from spendable cash
        "next": nxt,
        "next_estimate": due_now,
        "next_days": (date.fromisoformat(nxt["due"]) - today).days if nxt else None,
        # honest caveat that travels with the number wherever it's displayed
        "caveat": "這是用毛收入 × 30% 抓的粗估，實際要扣掉可抵的工作支出跟里程；找會計師確認過再照那個數字走。",
    }


def next_deadline(today: date | None = None) -> dict | None:
    today = today or now().date()
    for d in DEADLINES:
        if date.fromisoformat(d["due"]) >= today:
            return d
    return None


def deadline_note(st: dict) -> str | None:
    """Plain-language nudge, or None when nothing is close enough to matter."""
    nxt, days = st.get("next"), st.get("next_days")
    if not nxt or days is None or days > 21:
        return None
    ca = ("加州這期是 0%，不用繳" if nxt["ca_pct"] == 0
          else f"加州這期要繳 {int(nxt['ca_pct'] * 100)}%")
    when = "今天" if days == 0 else f"還有 {days} 天"
    return (f"{nxt['due']} 是 {nxt['label']} 的預繳日（{when}）。"
            f"這段期間（{nxt['covers'][0]} ~ {nxt['covers'][1]}）我記到的工作收入，"
            f"照 {int(st['rate'] * 100)}% 抓大概 ${st['next_estimate']:,.0f}。{ca}。"
            f"目前預留的稅金有 ${st['outstanding']:,.0f}。")


async def find_prior_payments(session) -> list[dict]:
    """Hunt the ledger for tax payments already made — IRS / EFTPS / FTB descriptors.

    Momo remembers paying something in June; whatever rate that payment implies beats any
    estimate this module would invent."""
    rows = (await session.execute(select(Transaction))).scalars().all()
    out = []
    for t in rows:
        if t.amount >= 0:
            continue
        if t.category == PAID_CATEGORY or T.guess(t.merchant_desc) == PAID_CATEGORY:
            d = budget.eff_date(t)
            out.append({"date": d.isoformat() if d else None,
                        "amount": round(-t.amount, 2),
                        "desc": t.merchant_desc})
    return sorted(out, key=lambda x: x["date"] or "")

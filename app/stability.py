"""How much emergency fund Momo needs — measured, not guessed.

"$10,000" and "$20,000" were both numbers someone typed once. Neither is wrong so much as
arbitrary: the right size of an emergency fund depends on how unstable the income actually
is, and Momo's instability is a thing we can measure from her own history.

The rule everyone quotes is "three to six months of expenses", with the wide end for
freelancers. This turns that into arithmetic:

    months needed = 3  +  how erratic the income is  +  how long the dry spells run

Both adjustments come from her recorded months, both are capped, and the whole thing is
reported component-by-component. A target that moves without explaining itself is just
moving goalposts, and she'd be right to stop believing it.

Recomputed every half-month, so her standing on the ladder changes as her situation does.
Steadier work lowers the bar and she climbs a rung without saving another dollar; a bad
stretch raises it and she drops one — which is exactly the warning she'd want.
"""
from __future__ import annotations

import statistics
from collections import defaultdict
from datetime import date

from sqlalchemy import select

from . import budget
from .config import now
from .db import get_kv, set_kv
from .models import Transaction

#: everyone needs this much regardless of how steady the work is
BASE_MONTHS = 3.0
#: and nobody sensibly holds more than this in cash
MAX_MONTHS = 9.0

#: how many months of history to judge from — long enough that one big gig can't swing it
WINDOW_MONTHS = 12

#: a month counts as "dry" below this share of a typical month
DRY_SHARE = 0.20

#: the target may not jump more than this in one recomputation, so it reads as a moving
#: assessment rather than a moving goalpost
MAX_STEP = 0.15

LAST_KEY = "cfg_emergency_target_last"


def _months_back(today: date, n: int) -> list[str]:
    y, m = today.year, today.month
    out = []
    for _ in range(n):
        m -= 1
        if m == 0:
            y, m = y - 1, 12
        out.append(f"{y:04d}-{m:02d}")
    return sorted(out)


async def income_by_month(session, today: date | None = None) -> dict[str, float]:
    """Recorded income per calendar month, zeros included, current month excluded.

    The running month is left out on purpose — a month that is only a third over always
    looks like a bad month, and would drag the volatility estimate upward forever."""
    today = today or now().date()
    rows = (await session.execute(select(Transaction).where(Transaction.amount > 0))).scalars().all()
    got: dict[str, float] = defaultdict(float)
    for t in rows:
        if not budget.is_income(t):
            continue
        d = budget.eff_date(t)
        if d:
            got[d.strftime("%Y-%m")] += t.amount
    if not got:
        return {}
    months = _months_back(today, WINDOW_MONTHS)
    earliest = min(got)
    return {m: round(got.get(m, 0.0), 2) for m in months if m >= earliest}


def assess(series: dict[str, float], survival_monthly: float) -> dict:
    """Turn a run of monthly income into a target, showing every step of the reasoning."""
    vals = list(series.values())
    if len(vals) < 3:
        months = BASE_MONTHS
        return {
            "months": months,
            "target": round(survival_monthly * months, 2),
            "confidence": "low",
            "why": ["紀錄還不夠久（少於三個月），先用最低標準三個月。"],
            "components": {"base": BASE_MONTHS, "volatility": 0.0, "drought": 0.0},
            "stats": {"n_months": len(vals)},
        }

    mean = sum(vals) / len(vals)
    sd = statistics.pstdev(vals)
    cv = (sd / mean) if mean > 0 else 0.0
    med = statistics.median(vals)

    # how erratic: a coefficient of variation of 1.0 means the swing is as big as the
    # average month, which is genuinely unstable and worth two extra months of cushion
    volatility = min(3.0, round(cv * 2.0, 2))

    # how long the gaps run: consecutive months earning under a fifth of a typical month
    dry_cut = med * DRY_SHARE
    longest = cur = 0
    for v in vals:
        cur = cur + 1 if v < dry_cut else 0
        longest = max(longest, cur)
    drought = float(min(3.0, longest))

    months = max(BASE_MONTHS, min(MAX_MONTHS, BASE_MONTHS + volatility + drought))
    target = round(survival_monthly * months / 250) * 250   # to a readable step

    why = [
        f"基本盤三個月：任何人都需要，$={survival_monthly:,.0f}/月 × 3。",
        (f"收入起伏：這 {len(vals)} 個月平均 ${mean:,.0f}、上下差 ${sd:,.0f}"
         f"（差距是平均的 {cv:.2f} 倍），加 {volatility:.1f} 個月。"),
    ]
    if drought:
        why.append(f"空窗期：最長連續 {longest} 個月幾乎沒進帳，加 {drought:.0f} 個月。")
    else:
        why.append("空窗期：目前沒有連續乾旱的月份，不加。")

    return {
        "months": round(months, 1),
        "target": float(target),
        "confidence": "high" if len(vals) >= 8 else "medium",
        "why": why,
        "components": {"base": BASE_MONTHS, "volatility": volatility, "drought": drought},
        "stats": {"n_months": len(vals), "mean": round(mean, 2), "stdev": round(sd, 2),
                  "cv": round(cv, 2), "median": round(med, 2),
                  "longest_dry_months": longest, "series": series},
    }


async def emergency_target(session, survival_monthly: float,
                           today: date | None = None) -> dict:
    """The computed target, damped so it can't lurch between periods."""
    series = await income_by_month(session, today)
    out = assess(series, survival_monthly)

    prev_raw = await get_kv(session, LAST_KEY)
    try:
        prev = float(prev_raw) if prev_raw else None
    except (TypeError, ValueError):
        prev = None

    if prev and prev > 0:
        lo, hi = prev * (1 - MAX_STEP), prev * (1 + MAX_STEP)
        damped = min(hi, max(lo, out["target"]))
        if abs(damped - out["target"]) > 1:
            out["why"].append(
                f"（一次最多只調 {int(MAX_STEP * 100)}%，所以這期先從 ${prev:,.0f} "
                f"走到 ${damped:,.0f}，不是一口氣跳到 ${out['target']:,.0f}。）")
        out["uncapped_target"] = out["target"]
        out["target"] = round(damped / 250) * 250
    out["previous"] = prev
    return out


async def remember_target(session, target: float) -> None:
    """Called once per period close, so the damping has something to step from."""
    await set_kv(session, LAST_KEY, str(round(float(target), 2)))

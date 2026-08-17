"""Weekly / monthly / quarterly reports, voiced by 秀琴阿姨."""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from . import budget, line_client, llm, memory
from . import taxonomy as T
from .config import aware, now
from .db import get_kv
from .models import Transaction

_EXCLUDE = budget.NON_SPEND_CATEGORIES | set()
_RANGES = {"weekly": 7, "monthly": 30, "quarterly": 90}


async def gather(session, start, end) -> dict:
    """What the window actually held — with HER money kept apart from money that merely
    passed through her hands.

    One gross total called 「支出」 is how a week where she fronted $917 of production
    costs and paid a $340 DMV bill got reported as 「花超兇」 in the same message that
    said her daily budget was barely touched. Neither half was wrong; adding them was.
    工作 spending is a business cost someone else usually repays, 不規則 is a shock she
    didn't choose, and only what :func:`budget.is_discretionary` counts is the money the
    line is actually about. Same split the engine uses, so the report can't contradict
    the dashboard.
    """
    rows = (await session.execute(select(Transaction))).scalars().all()
    cats: dict[str, float] = {}
    merchants: dict[str, float] = {}
    income = 0.0
    mine = work = irregular = fixed = 0.0
    lo = start.date() if hasattr(start, "date") else start
    hi = end.date() if hasattr(end, "date") else end
    for t in rows:
        # eff_date, so a refund lands with the charge it reverses — the same rule the
        # budget and the dashboard use.
        d = budget.eff_date(t)
        if d is None:
            fallback = aware(t.posted_at or t.created_at)
            d = fallback.date() if fallback else None
        if d is None or not (lo <= d < hi):
            continue
        if budget.is_income(t):
            # a raw amount>0 counted her mother's paybacks, refunds and her own transfers
            # as pay — the monthly report told her she earned $4,088 in a month she
            # earned $1,585. Real deposits only.
            income += t.amount
        elif budget.is_spend(t):
            amt = budget.spend_amount(t)
            tr = T.treatment(t.category)
            if budget.is_discretionary(t):
                mine += amt
            elif tr == T.WORK:
                work += amt
            elif tr == T.IRREGULAR:
                irregular += amt
            else:
                fixed += amt
            c = t.category or "未分類"
            cats[c] = cats.get(c, 0.0) + amt
            m = t.merchant_desc or "?"
            merchants[m] = merchants.get(m, 0.0) + amt
    return {
        "income": round(income, 2),
        "spend": round(mine + work + irregular + fixed, 2),   # gross, kept for callers
        "mine": round(mine, 2),
        "work": round(work, 2),
        "irregular": round(irregular, 2),
        "fixed": round(fixed, 2),
        "by_cat": sorted(cats.items(), key=lambda x: -x[1]),
        "top": sorted(merchants.items(), key=lambda x: -x[1])[:5],
    }


def _fmt(data: dict, bud: dict | None) -> str:
    # Category IDs are internal. Printing them raw is how she ended up saying
    # 「work 花 917、fees 花 340」 to a person who has never seen the taxonomy.
    lines = [f"自己花的（吃住玩，會吃到額度） ${data.get('mine', 0):.2f}",
             f"收入 ${data['income']:.2f}"]
    if data.get("work"):
        lines.append(f"替劇組墊的工作支出 ${data['work']:.2f}"
                     "（這不是她的花費，多數之後會拿回來，不要算在「花太多」裡）")
    if data.get("irregular"):
        lines.append(f"一次性的不規則支出 ${data['irregular']:.2f}（規費、醫療那類，不是日常決定）")
    if data.get("fixed"):
        lines.append(f"固定開銷 ${data['fixed']:.2f}（房租、訂閱那類，本來就會扣）")
    if data["by_cat"]:
        lines.append("分類：" + "、".join(f"{T.label(c)} ${a:.0f}" for c, a in data["by_cat"]))
    if data["top"]:
        lines.append("花最多的商家：" + "、".join(f"{m} ${a:.0f}" for m, a in data["top"]))
    if bud is None:
        pass                                  # quarterly report has no budget line by design
    elif not bud.get("configured"):
        lines.append("（預算還沒設定，所以這裡沒有本期額度可以報。）")
    else:
        lines.append(
            f"本期預算 ${bud['allowance']:.0f}，已花 ${bud['spent']:.0f}，剩 ${bud['remaining']:.0f}"
        )
    return "\n".join(lines)


async def run_report(session, kind: str) -> str:
    today = now()
    start = aware(today - timedelta(days=_RANGES.get(kind, 30)))
    data = await gather(session, start, today)
    bud = await budget.status(session) if kind != "quarterly" else None
    text = await llm.report(kind, _fmt(data, bud))
    owner = await get_kv(session, "owner_user_id")
    if owner:
        await line_client.push(owner, text)
        await memory.remember(session, "assistant", text)  # so replies to the report land in context
    return text

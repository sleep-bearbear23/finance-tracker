"""Weekly / monthly / quarterly reports, voiced by 秀琴阿姨."""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from . import budget, line_client, llm, memory
from .config import aware, now
from .db import get_kv
from .models import Transaction

_EXCLUDE = budget.NON_SPEND_CATEGORIES | set()
_RANGES = {"weekly": 7, "monthly": 30, "quarterly": 90}


async def gather(session, start, end) -> dict:
    rows = (await session.execute(select(Transaction))).scalars().all()
    cats: dict[str, float] = {}
    merchants: dict[str, float] = {}
    income = 0.0
    spend = 0.0
    for t in rows:
        d = aware(t.posted_at or t.created_at)
        if not d or not (start <= d < end):
            continue
        if t.amount > 0:
            income += t.amount
        elif budget.is_spend(t):
            amt = abs(t.amount)
            spend += amt
            c = t.category or "未分類"
            cats[c] = cats.get(c, 0.0) + amt
            m = t.merchant_desc or "?"
            merchants[m] = merchants.get(m, 0.0) + amt
    return {
        "income": round(income, 2),
        "spend": round(spend, 2),
        "by_cat": sorted(cats.items(), key=lambda x: -x[1]),
        "top": sorted(merchants.items(), key=lambda x: -x[1])[:5],
    }


def _fmt(data: dict, bud: dict | None) -> str:
    lines = [f"支出合計 ${data['spend']:.2f}，收入 ${data['income']:.2f}"]
    if data["by_cat"]:
        lines.append("分類：" + "、".join(f"{c} ${a:.0f}" for c, a in data["by_cat"]))
    if data["top"]:
        lines.append("花最多的商家：" + "、".join(f"{m} ${a:.0f}" for m, a in data["top"]))
    if bud and bud["allowance"]:
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

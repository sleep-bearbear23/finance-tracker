"""Natural-language Q&A: assemble a compact data context, let 秀琴阿姨 answer."""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from . import budget, llm
from .config import aware, now
from .models import Account, Transaction


async def build_context(session) -> str:
    today = now()
    month_start = today.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    biweek_start = today - timedelta(days=14)

    spend = (await session.execute(
        select(Transaction).where(Transaction.amount < 0)
    )).scalars().all()

    def eff_date(t):
        return aware(t.posted_at or t.created_at)

    month_by_cat: dict[str, float] = {}
    biweek_total = 0.0
    for t in spend:
        d = eff_date(t)
        if d and d >= month_start:
            month_by_cat[t.category or "未分類"] = month_by_cat.get(t.category or "未分類", 0.0) + abs(t.amount)
        if d and d >= biweek_start:
            biweek_total += abs(t.amount)

    lines = ["本月各分類花費："]
    if month_by_cat:
        for cat, amt in sorted(month_by_cat.items(), key=lambda x: -x[1]):
            lines.append(f"  - {cat}: ${amt:.2f}")
    else:
        lines.append("  （本月還沒有支出紀錄）")
    lines.append(f"本月支出合計：${sum(month_by_cat.values()):.2f}")
    lines.append(f"最近 14 天支出：${biweek_total:.2f}")

    accts = (await session.execute(select(Account))).scalars().all()
    if accts:
        lines.append("帳戶餘額：")
        for a in accts:
            lines.append(f"  - {a.name}: ${a.balance:.2f}")

    try:
        b = await budget.status(session)
        if b["allowance"]:
            lines.append(
                f"本期預算（{b['period_start']}~{b['period_end']}）：可花 ${b['allowance']:.0f}，"
                f"已花 ${b['spent']:.0f}，剩 ${b['remaining']:.0f}，還有 {b['days_left']} 天；"
                f"每兩週存錢目標 ${b['savings_biweekly']:.0f}"
            )
    except Exception:
        pass

    recent = sorted(spend, key=eff_date, reverse=True)[:40]
    if recent:
        lines.append("最近的交易：")
        for t in recent:
            d = eff_date(t)
            ds = d.strftime("%m/%d") if d else "??"
            note = f"（{t.note}）" if t.note else ""
            lines.append(f"  {ds} ${abs(t.amount):.2f} {t.merchant_desc} [{t.category or '未分類'}]{note}")

    return "\n".join(lines)


async def answer(session, question: str) -> str:
    ctx = await build_context(session)
    return await llm.answer_question(question, ctx)

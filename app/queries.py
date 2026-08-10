"""Natural-language Q&A: assemble a compact data context, let 秀琴阿姨 answer."""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from . import budget, llm, memory, networth, prefs
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

    # Net worth: one authoritative figure that already includes Apple and anything else the
    # bank sync can't reach, so she never answers "身家" off the Chase balances alone.
    nw = {}
    try:
        nw = await networth.compute(session)
        if nw["rows"]:
            lines.append("你名下所有帳戶（Chase 是銀行自動同步的，Apple 那些是默默自己報的）：")
            for r in nw["rows"]:
                src = r.get("src", "")
                lines.append(f"  - {r['name']}（{r['kind']}{'·同步' if src=='同步' else ''}）: ${abs(r['amount']):.2f}")
            lines.append(
                f"淨資產 ＝ 現金/存款合計 ${nw['assets']:.2f} − 卡債/欠款 ${nw['debts']:.2f} "
                f"＝ ${nw['net']:.2f}。"
                "（默默問淨資產／身家／net worth 就報這個數字。Apple 那些他自己給的也已經算進去了，"
                "不要只算 Chase、也不要漏掉卡債。）"
            )
    except Exception:
        pass

    accts = (await session.execute(select(Account))).scalars().all()
    if accts and nw.get("source") == "ledger":
        lines.append("（銀行即時餘額，僅供參考，已經反映在上面的淨資產快照裡，不要重複加）：")
        for a in accts:
            lines.append(f"  - {a.name}: ${a.balance:.2f}")

    # Pending invoices: money Momo is owed / expects soon but that hasn't actually landed yet.
    # This is REAL money-in-waiting — different from the budget income basis. Answer "還沒收到的
    # 薪水 / 待收款 / 入帳後我會有多少" from THIS list, never from the income basis.
    try:
        pend = await prefs.pending_invoices(session)
        if pend:
            today_ym = now().strftime("%Y-%m")
            total = 0.0
            lines.append("還沒入帳的預期收入（待收款，錢還沒真的進來、還在等對方付）：")
            for p in pend:
                amt = float(p.get("amount") or 0)
                total += amt
                nm = p.get("note") or "某案"
                wn = p.get("when") or "時間未定"
                overdue = "，這筆照理該進來了、幫默默確認收到沒" if (p.get("when") and str(p.get("when"))[:7] < today_ym) else ""
                lines.append(f"  - {nm}：${amt:.2f}，預計 {wn}{overdue}")
            cash_now = (nw.get("assets", 0.0) - nw.get("debts", 0.0)) if nw else 0.0
            lines.append(f"待收款合計：${total:.2f}。")
            lines.append(
                f"如果這些都入帳，默默手上大概會有 ${cash_now + total:.2f}"
                f"（現在淨資產 ${cash_now:.2f} ＋ 待收款 ${total:.2f}）。"
            )
            lines.append(
                "（默默問「還沒收到的薪水／待收款／有多少在路上／入帳後會有多少」，就照這個清單和合計老實回答他，"
                "這是他實際被欠的錢，跟下面預算用的收入基準是兩回事，不要混、也不要跟他說你不清楚。）"
            )
    except Exception:
        pass

    try:
        b = await budget.status(session)
        basis = (
            f"收入基準 ${b['income_biweekly']:.0f}／兩週"
            f"（預期 ${b['income_expected']:.0f}＋實際入帳 ${b['income_actual']:.0f} 各半抓的）"
        )
        lines.append(
            f"本期預算（{b['period_start']}~{b['period_end']}）算法："
            f"{basis}，減固定支出 ${b['fixed_biweekly']:.0f}，"
            f"減存錢目標 ${b['savings_biweekly']:.0f}，等於可花 ${b['allowance']:.0f}；"
            f"這期已花 ${b['spent']:.0f}，剩 ${b['remaining']:.0f}，還有 {b['days_left']} 天。"
            "（這個收入基準只是用來抓每期能花多少的，不是默默實際被欠的錢；他問待收款請看上面那份清單。"
            "如果他問預算怎麼算，就照這幾個數字誠實拆給他看，不要自己另外加減。）"
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
    convo = await memory.recent(session, 8)
    return await llm.answer_question(question, ctx, convo)

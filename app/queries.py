"""Natural-language Q&A: assemble a compact data context, let 秀琴阿姨 answer."""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from . import allowance as AL, budget, llm, memory, networth, prefs, taxonomy
from .config import aware, now
from .models import Account, Transaction


async def build_context(session) -> str:
    today = now()
    month_start = today.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    biweek_start = today - timedelta(days=14)

    # Through the shared predicates, not a raw amount<0 scan. The raw scan counted the
    # Apple Card payment and every transfer as spending, so 陳會計 quoted 本月支出
    # $4,266 in a month the real figure was $926 — and she quotes this out loud, on the
    # write channel, where being wrong costs trust fastest.
    from . import budget
    spend = (await session.execute(
        select(Transaction).where(Transaction.amount < 0)
    )).scalars().all()

    month_by_cat: dict[str, float] = {}
    biweek_total = 0.0
    for t in spend:
        if not budget.is_spend(t):
            continue
        d = budget.eff_date(t)
        if d and d >= month_start.date():
            ck = taxonomy.label(t.category) if t.category else "未分類"
            month_by_cat[ck] = month_by_cat.get(ck, 0.0) + abs(t.amount)
        if d and d >= biweek_start.date():
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
            cash_now = (nw.get("spendable", 0.0) - nw.get("debts", 0.0)) if nw else 0.0
            lines.append(f"待收款合計：${total:.2f}。")
            lines.append(
                f"如果這些都入帳，默默手上可動用的現金大概會變成 ${cash_now + total:.2f}"
                f"（現在可動用 ${cash_now:.2f} ＋ 待收款 ${total:.2f}）。"
                "（注意：可動用現金不等於淨資產——投資帳戶不算在可動用裡。）"
            )
            lines.append(
                "（默默問「還沒收到的薪水／待收款／有多少在路上／入帳後會有多少」，就照這個清單和合計老實回答他，"
                "這是他實際被欠的錢，跟下面預算用的收入基準是兩回事，不要混、也不要跟他說你不清楚。）"
            )
    except Exception:
        pass

    # Every fixed cost, itemised. She could see the TOTAL and nothing else, so when Momo
    # asked her to break down $2,521 she had to say she could not — and worse, when he asked
    # her to add 「給家人的錢 $1,000／月」 she could not see 「房租（Zelle 給媽媽）$1,000／月」
    # already sitting there, and his rent got counted twice.
    try:
        from . import fixed as FX
        frows = await FX.rows(session, include_sinking=False)
        if frows:
            lines.append("固定開銷（每一筆，這是預算每期扣掉的東西）：")
            for r in sorted(frows, key=lambda x: -x["monthly"]):
                cad = {"monthly": "每月", "quarterly": "每季", "semiannual": "每半年",
                       "annual": "每年"}.get(r.get("cadence") or "monthly", "每月")
                per = f"，換算每月 ${r['monthly']:.2f}" if (r.get("cadence") or "monthly") != "monthly" else ""
                due = f"，下次 {r['next_due']}" if r.get("next_due") else ""
                hand = "，要自己轉" if r.get("manual") else ""
                lines.append(f"  - {r['name']}：{cad} ${float(r.get('amount') or 0):.2f}{per}{due}{hand}")
            lines.append(f"固定開銷合計：每月 ${await FX.monthly_total(session, include_sinking=False):.2f}。")
            lines.append(
                "（默默問固定開銷的明細、breakdown、有哪些訂閱，就照這張清單一筆一筆念給他，"
                "不要跟他說你只有總數。要新增一筆之前也先看這張表——"
                "同一筆錢用不同名字加第二次，預算會每個月多扣一份。）")
    except Exception:
        pass

    try:  # 罐子：小，但這是她能主動看到「某個罐子存滿了」的唯一途徑
        from . import jars as JR
        js = await JR.load(session)
        if js:
            bits = []
            for j in js:
                bal, tgt = float(j.get("balance") or 0), j.get("target")
                mark = "（滿了）" if tgt and bal >= float(tgt) - 0.01 else ""
                bits.append(f"{j.get('name')} ${bal:,.0f}"
                            + (f"/${float(tgt):,.0f}{mark}" if tgt else ""))
            lines.append("罐子（有主的錢，已經從可花的錢裡扣掉了）：" + "、".join(bits))
            lines.append("（罐子是他自己設的目標，不是帳單。有罐子存到目標了就講一聲，"
                         "但不要接著叫他拿去做什麼。）")
    except Exception:
        pass

    try:  # 他自己說過的話——結算時寫的回顧
        from . import settle as ST
        last = await ST.last_reflection(session)
        if last and last.get("answers"):
            said = "；".join(f"{v}" for v in last["answers"].values() if v)
            if said:
                lines.append(f"他上次結算（{last['label']}）自己寫的：{said}")
                lines.append("（如果這期的數字真的顯示他做到了他說要改的事，講出來。"
                             "數字沒顯示就不要說他做到了。）")
    except Exception:
        pass

    try:
        a = await AL.compute(session)
        if a.get("awaiting_settlement"):
            aw = a["awaiting_settlement"]
            # She must not quote a budget the ritual hasn't produced yet. The write
            # channel is the one place being wrong costs trust fastest.
            lines.append(
                f"⚠ {aw['label']} 還沒結算，所以這一期【沒有額度可以講】。"
                "默默問還能花多少，就說還沒結算、要先決定口袋那筆錢去哪，"
                "叫他去結算頁面（她會拿到連結）。不要自己估一個數字給他，也不要"
                "拿上一期的數字充數。記帳、問這筆是什麼、他自己報的花費，全部照常。")
        lines.append(
            f"本期預算（{a['period_start']}~{a['period_end']}，{a['period_label']}，"
            f"共 {a['days_in_period']} 天）：可花 ${a['allowance']:.0f}，已花 ${a['spent']:.0f}，"
            f"剩 ${a['remaining']:.0f}，還有 {a['days_left']} 天"
            + (f"，平均每天 ${a['per_day_left']:.0f}。" if a.get("per_day_left") is not None else "。")
        )
        dy = a.get("daily") or {}
        if dy:
            lines.append(
                f"每天的線：一天 ${dy['daily_base']:.0f}"
                + (f"（今天加碼 ${dy['daily_bump']:.0f}，可以花 ${dy['daily_today']:.0f}）"
                   if dy.get("daily_bump") else "")
                + f"，今天已經花 ${dy['today_spent']:.0f}，還剩 ${dy['today_left']:.0f}。"
                f"本期口袋 ${dy['pool']:.0f}。"
            )
            lines.append(
                "（口袋是前面幾天沒花完的錢。他問今天能花多少，就講「今天還能花」那個數字，"
                "不要講整期剩多少——整期的數字會讓他以為最後一天可以一次花完。"
                "他想多花一點就用 raise_daily，錢只能從口袋出；口袋不夠就照實說不夠，"
                "那不是小氣，是那筆錢還沒省出來。）"
            )
        lines.append("這個數字是三個角度一起看、取最緊的那個：")
        for extra in AL.explain(a):        # explain() already walks all three lenses
            lines.append(f"  · {extra}")
        lines.append(
            f"水位：可動用 ${a['reserve_total']:.0f}（現金扣掉卡債跟預留的稅），"
            f"站在{a['standing_rung']['name'] if a['standing_rung'] else '水位以下'}，"
            f"預計要撐 {a['periods_to_money']} 期才有下一筆錢進來。"
        )
        # The dashboard stops calling the line a budget when it drops below what eating
        # costs; 陳會計 has to stop too, or the fix only holds on one screen. Her card said
        # 「還能花 $4」 with five days left, and saying that out loud in LINE would be the
        # same unfollowable instruction in a friendlier voice.
        try:
            from . import analytics as AN
            from . import period as P
            te2 = await AN.to_earn(session, AN.HORIZON_MONTHS)
            _pot, _pot_bal = AN._dip_pot(a)
            dv = AN.dip_view(a["allowance"], a["spent"], a["days_left"],
                             P.days_in(a["period_key"]), te2["lean_flex_monthly"],
                             a.get("binding") or "", coverage=a.get("coverage", 1.0),
                             pot=_pot, pot_balance=_pot_bal)
            if dv["mode"] == "dip":
                lines.append(
                    f"⚠ 這期的線已經低於「最省也要花的錢」了：剩 {a['days_left']} 天最省要 "
                    f"${dv['survival_need']:.0f}（一天 ${dv['survival_per_day']:.0f}），"
                    f"線上只剩 ${dv['line_left']:.0f}，差 ${dv['dip']:.0f}。"
                    + ("這條線本來就不夠吃飯，不是他花太兇。" if dv["cause"] == "line"
                       else "這期的線本來夠用，是已經花掉了。")
                    + f"缺的照規則從{dv['pot']}拿。"
                    + "（這種時候不要跟他講「還能花 $X、一天 $Y」——那個數字他做不到，"
                    "講出來只會像在罵他。要講的是：撐完這幾天大概要多少、差的那筆會從"
                    "緊急預備金出、然後能動的是催款跟壓花費。）")
        except Exception:
            pass
        lines.append(
            "（阿姨的週期是每月 1–15 號、16–月底。預估收入只會用來算「要撐多久」，"
            "永遠不會拿來把可花的錢調高——這是默默定的規矩。"
            "如果他問預算怎麼算，就照上面三個角度誠實拆給他看，不要自己另外加減。）"
        )
    except Exception:
        pass

    # 12, not 40. The standing block was enormous because she had no way to ASK; with
    # look_up she can fetch more the moment a question needs it, which is both shorter
    # and fresher than pre-loading four dozen rows into every single turn.
    recent = sorted(spend, key=lambda t: budget.eff_date(t) or now().date(), reverse=True)[:12]
    if recent:
        lines.append("最近的交易：")
        for t in recent:
            d = budget.eff_date(t)
            ds = d.strftime("%m/%d") if d else "??"
            note = f"（{t.note}）" if t.note else ""
            lines.append(f"  {ds} ${abs(t.amount):.2f} {t.merchant_desc} [{taxonomy.label(t.category) if t.category else '未分類'}]{note}")
        lines.append("（只列最近 12 筆。要更多、要某家店的歷史、某個分類花多少、"
                     "待收款明細、罐子細節——用 look_up 查，不要用猜的，也不要叫他重講一次。）")

    return "\n".join(lines)

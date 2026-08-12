"""The numbers the detail pages are built on — one module so no page invents its own.

The overview was carrying five jobs at once. These are the computations the separate
pages need, all derived from :mod:`app.facts` and the same budget helpers, so a figure on
the 計畫 page can never disagree with the same figure on the overview.

The centrepiece is :func:`to_earn` — "how much more do I need to make in the next three
months". Momo asked for it as an index she can hold up against a gig offer, which means
it has to be a small number of dollars and a count of jobs, not a spreadsheet.
"""
from __future__ import annotations

import re
import statistics
from collections import defaultdict
from datetime import date, timedelta

from . import allowance as AL
from . import budget
from . import facts as F
from . import fixed as FX
from . import period as P
from . import stability as STAB
from . import tax as TAX
from . import taxonomy as T
from .config import now

#: months of runway the index plans for — long enough to be worth chasing work over,
#: short enough that Momo can actually picture it
HORIZON_MONTHS = 3

#: an emergency-fund gap is closed over a year, not a quarter; the index only asks for
#: this horizon's slice of it
EMERGENCY_YEARS = 1.0


# ── spending, sliced the way the 計畫 page reads it ───────────────────
async def category_series(session, n_periods: int = 12, f: F.Facts | None = None) -> dict:
    """Per-category spend for each of the last N half-months, plus totals and shares.

    Only categories that eat the allowance or are otherwise real spending appear; skip
    rows (transfers, tax) are excluded, which is why the shares actually add to 100%."""
    f = f or await F.build(session)
    keys = P.last_n(budget.current_key(), n_periods)
    lo, _ = P.key_bounds(keys[0])
    _, hi = P.key_bounds(keys[-1])

    per: dict[str, dict[str, float]] = defaultdict(lambda: dict.fromkeys(keys, 0.0))
    total = 0.0
    for t in f.txns:
        if not budget.is_spend(t):
            continue
        d = budget.eff_date(t)
        if not d or d < lo or d > hi:
            continue
        k = P.key_for(d)
        if k not in per[t.category or "未分類"]:
            continue
        amt = budget.spend_amount(t)
        per[t.category or "未分類"][k] += amt
        total += amt

    rows = []
    for cat, series in per.items():
        s = round(sum(series.values()), 2)
        vals = [round(series[k], 2) for k in keys]
        rows.append({
            "category": cat, "label": T.label(cat) if cat != "未分類" else cat,
            "treatment": T.treatment(cat),
            "treatment_label": T.TREATMENT_LABEL.get(T.treatment(cat) or "", ""),
            "total": s, "share": round(100 * s / total, 1) if total else 0.0,
            "per_period": round(s / n_periods, 2),
            "series": vals,
        })
    rows.sort(key=lambda r: -r["total"])

    by_treat: dict[str, float] = defaultdict(float)
    for r in rows:
        by_treat[r["treatment"] or "未分類"] += r["total"]

    return {
        "keys": keys,
        "labels": [P.label(k) for k in keys],
        "month_start": [P.is_month_start(k) for k in keys],
        "rows": rows,
        "total": round(total, 2),
        "by_treatment": [
            {"treatment": k, "label": T.TREATMENT_LABEL.get(k, k), "total": round(v, 2),
             "share": round(100 * v / total, 1) if total else 0.0}
            for k, v in sorted(by_treat.items(), key=lambda x: -x[1])
        ],
    }


#: everything a bank puts around a payer's name — the word "payroll", the month it was
#: for, the entity suffix. "AVG MAY Payroll" and "AVG Payroll APRIL" are one client, and
#: listing them as two hid that AVG is worth $1,400 a year to Momo.
#: A month may carry the year glued to it ("AVG MAR26"), which is why the month group
#: takes trailing digits. This is aggressive on purpose — the raw bank description is
#: still printed on every row of 入帳紀錄, so nothing is actually lost.
_PAYER_NOISE = re.compile(
    r"\b(payroll|pay\s*roll|day\s*rate|zelle\s*(payment)?\s*from|payment\s*from|"
    r"direct\s*dep(osit)?|deposit|invoice|inv|"
    r"(january|february|march|april|may|june|july|august|september|october|november|december|"
    r"jan|feb|mar|apr|jun|jul|aug|sept?|oct|nov|dec)\d{0,4}|"
    r"inc|llc|ltd|co)\b", re.I)


def payer_name(desc: str) -> str:
    """A payer's name with the bank's packaging taken off.

    Deliberately conservative: if stripping leaves nothing, keep the original. A blank
    row is worse than an ugly one."""
    s = re.sub(r"\bx{3,}\d*\b", " ", desc or "", flags=re.I)   # masked account numbers
    s = re.sub(r"\b\d{5,}\b", " ", s)                          # trace / reference numbers
    s = s.replace("_", " ")
    s = _PAYER_NOISE.sub(" ", s)
    s = re.sub(r"[,.\-–—#:/]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip(" ·")
    return (s or (desc or "?").strip())[:32]


def _month_span(observed: list[str]) -> list[str]:
    """Every YYYY-MM from the first observed month to the current one, gaps included.

    Only inside the observed range — inventing months before the ledger starts would
    invent zeros we have no evidence for."""
    if not observed:
        return []
    y, m = int(observed[0][:4]), int(observed[0][5:7])
    today = now().date()
    end = (today.year, today.month)
    out = []
    while (y, m) <= end:
        out.append(f"{y:04d}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


# ── income, sliced three ways ────────────────────────────────────────
async def income_performance(session, f: F.Facts | None = None) -> dict:
    """Real pay by half-month, by month and by year, plus the payer mix.

    Uses :func:`budget.is_income`, so reimbursements and a friend's Zelle stay out of it —
    this is earnings, not deposits."""
    f = f or await F.build(session)
    halves: dict[str, float] = defaultdict(float)
    months: dict[str, float] = defaultdict(float)
    years: dict[str, float] = defaultdict(float)
    payers: dict[str, float] = defaultdict(float)
    payer_n: dict[str, int] = defaultdict(int)
    rows = []
    for t in f.txns:
        if not budget.is_income(t):
            continue
        d = budget.eff_date(t)
        if not d:
            continue
        halves[P.key_for(d)] += t.amount
        months[d.strftime("%Y-%m")] += t.amount
        years[d.strftime("%Y")] += t.amount
        pn = payer_name(t.merchant_desc or "?")
        payers[pn] += t.amount
        payer_n[pn] += 1
        rows.append({"date": d.isoformat(), "name": t.merchant_desc,
                     "amount": round(t.amount, 2), "source": t.source, "note": t.note})
    rows.sort(key=lambda r: r["date"], reverse=True)

    hkeys = P.last_n(budget.current_key(), 12)
    # A month with no income is a month with no income — it has to appear, and it has to
    # count in the median. Skipping it drew June straight into July on the chart and
    # quietly raised the median month by $280.
    mkeys = _month_span(sorted(months))[-12:]
    # …but the month we are standing in is half-finished, so it belongs on the chart and
    # not in the median. On the 11th it would otherwise vote "$0" against seven real months.
    this_month = now().strftime("%Y-%m")
    mvals = [months.get(k, 0.0) for k in mkeys if k != this_month]
    # same argument one level down: the half we are living in is not a data point yet
    here = budget.current_key()
    hvals = [halves.get(k, 0.0) for k in hkeys if k != here]

    return {
        "halves": [{"key": k, "label": P.label(k), "month_start": P.is_month_start(k),
                    "amount": round(halves.get(k, 0.0), 2)} for k in hkeys],
        "months": [{"month": k, "amount": round(months.get(k, 0.0), 2)} for k in mkeys],
        "years": [{"year": k, "amount": round(v, 2)} for k, v in sorted(years.items())],
        "payers": [{"name": k, "amount": round(v, 2), "n": payer_n[k]}
                   for k, v in sorted(payers.items(), key=lambda x: -x[1])[:12]],
        "rows": rows,
        "median_month": round(statistics.median(mvals), 2) if mvals else 0.0,
        "median_half": round(statistics.median(hvals), 2) if hvals else 0.0,
        "median_payment": round(statistics.median([r["amount"] for r in rows]), 2) if rows else 0.0,
        "n_payments": len(rows),
    }


# ── the index Momo holds up against a gig offer ──────────────────────
async def to_earn(session, months: int = HORIZON_MONTHS,
                  f: F.Facts | None = None) -> dict:
    """How much has to land in the next `months` for each of three standards to hold.

      生存  fixed costs and a lean-but-livable flex floor. Nothing else.
      持平  fixed costs and how Momo actually lives. Break even, save nothing.
      目標  the above, plus the savings target, plus this horizon's slice of the
            emergency-fund gap.

    Every tier is grossed up for tax — the money has to arrive before it is taxed — and
    then reduced by work already done but not yet paid for, because that money is coming
    whether or not she books anything new."""
    f = f or await F.build(session)
    a = await AL.compute(session)
    tax_st = a["tax"]
    fixed_monthly = await FX.monthly_total(session)

    # What living actually costs, per month, from her own record. The middle tier was
    # already live — it is her recent half-month median doubled — but the floor was a
    # constant I typed ($550) with a comment claiming it came from her cheapest months.
    # Nothing recomputed it. Now the 25th percentile of her real flexible half-months does,
    # so the floor is a number she has actually lived on, and it moves as she does.
    flex = await FX.observed_flex(session)
    if flex.get("enough") and flex["lean"] > 0:
        lean_flex, normal_flex = flex["lean"], flex["median"]
        lean_basis = (f"近 {flex['periods']} 期實際彈性支出：最省的四分之一 / 中位數")
    else:
        lean_flex = AL.LEAN_FLEX_MONTHLY
        normal_flex = round((a["recent_median"] or 0) * 2, 2) or lean_flex
        lean_basis = "預設值，資料還不夠算"
    # Both tiers must come off the SAME series. Taking the floor from one population
    # (all non-fixed spend) and the middle from another (only what eats the allowance)
    # produced 生存 $12,144 against 持平 $11,542 — surviving costing more than breaking
    # even, which is not a thing that can be true.
    lean_flex = min(lean_flex, normal_flex)
    # a["savings_period"] is what survived this period's soft-savings skip; the index has
    # to plan for the intention, or a lean fortnight would quietly lower the whole target
    from . import prefs
    pr = await prefs.get_prefs(session)
    savings_monthly = round(budget.savings_for(pr["savings_amount"], pr["savings_cadence"],
                                               budget.current_key(), 0.0) * 2, 2)

    emerg = a["emergency"]
    fund_now = _emergency_balance(f)
    gap = max(0.0, (emerg.get("target") or 0) - fund_now)
    emerg_slice = round(gap * (months / (12 * EMERGENCY_YEARS)), 2)

    pend_items = await _pending(session)
    pending = _pending_within(pend_items, months)            # after the lateness haircut
    pending_face = round(sum(float(p.get("amount") or 0) for p in pend_items), 2)

    rate = tax_st["rate"]

    def tier(name: str, net_monthly: float, extra: float, why: str) -> dict:
        """Net cost of living → grossed up for tax → then, and only then, reduced by money
        already owed.

        The order matters and is the answer to "is it literally minus $10k". It is not:
        pending is subtracted from the GROSSED-UP figure, because an invoice is gross
        revenue and tax will be owed on it. Taking it off the net side would have credited
        her with 30% more spending power than the cheque actually delivers.

        `need` leans on money that has not arrived. `bare` is the same number with none of
        it counted — what she has to earn if every production keeps stalling. Both are
        reported, because one of them is a plan and the other is the floor."""
        net = net_monthly * months + extra
        gross = net / (1 - rate) if rate < 1 else net
        need = max(0.0, gross - pending)
        return {"name": name, "net": round(net, 2), "gross": round(gross, 2),
                "need": round(need, 2), "per_month": round(need / months, 2),
                "bare": round(gross, 2), "bare_per_month": round(gross / months, 2),
                "why": why}

    tiers = [
        tier("生存", fixed_monthly + lean_flex, 0.0,
             f"固定 ${fixed_monthly:,.0f} ＋ 最省的生活 ${lean_flex:,.0f}／月"),
        tier("持平", fixed_monthly + normal_flex, 0.0,
             f"固定 ${fixed_monthly:,.0f} ＋ 你實際的花法 ${normal_flex:,.0f}／月"),
        tier("目標", fixed_monthly + normal_flex + savings_monthly, emerg_slice,
             f"再加存錢 ${savings_monthly:,.0f}／月，"
             f"跟這一季該補的緊急預備金 ${emerg_slice:,.0f}"),
    ]

    perf = await income_performance(session, f)
    # "16.1 個案子" was $8,877 ÷ the median DEPOSIT ($550) — a population that includes a
    # $185 Zelle from a friend and a $300 flat-pay day. The unit Momo thinks in is a
    # booking, and she now records those, so the median booking is the honest denominator.
    # It falls back to the median payment while there are too few bookings to be a median.
    book = sorted(float(p.get("amount") or 0) for p in pend_items)
    med_gig = round(statistics.median(book), 2) if len(book) >= 2 else 0.0
    med = perf["median_payment"] or 0.0
    unit = med_gig or med
    for t in tiers:
        t["gigs"] = round(t["need"] / unit, 1) if unit > 0 else None
        t["bare_gigs"] = round(t["bare"] / unit, 1) if unit > 0 else None

    from . import prefs
    today = now().date()
    late = [{"note": p.get("note"), "amount": float(p.get("amount") or 0),
             "when": p.get("when"), "confidence": prefs.confidence(p, today),
             "stage": prefs.stage_of(p), "days": p.get("days"),
             "wrapped_on": p.get("wrapped_on"),
             "late_days": max(0, (today - (prefs.landing(p) or today)).days)}
            for p in pend_items]

    # A target in dollars is a target she cannot hold against a calendar. Momo: "have an
    # algorithm to calculate my average day rate the past three months and use that number
    # to calculate how many more work days I need."
    # pend_items already includes work she has SHOT but not been paid for, which is her
    # most recent evidence of what she charges — "day rate i think we could also consider
    # day rates of one's we shot but havent recieve the money yet."
    from . import seed_invoices as SI
    dr = prefs.day_rate(pend_items, _paid_with_days(f),
                        pinned=await prefs.pinned_day_rate(session),
                        invoices=await SI.invoices(session))
    booked_days = sum(int(p.get("days") or 0) for p in pend_items)
    if dr["rate"] > 0:
        for t in tiers:
            t["work_days"] = round(t["need"] / dr["rate"], 1)
            t["bare_work_days"] = round(t["bare"] / dr["rate"], 1)

    return {
        "months": months,
        "tax_rate": rate,
        "pending": round(pending, 2),
        "pending_face": pending_face,
        "pending_haircut": round(pending_face - pending, 2),
        "pending_items": sorted(late, key=lambda x: -x["late_days"]),
        "median_payment": med,
        "median_gig": med_gig,
        "gig_unit": unit,
        "day_rate": dr,
        "booked_days": booked_days,
        "lean_basis": lean_basis,
        "fixed_reconcile": await FX.reconcile(session),
        "fixed_monthly": fixed_monthly,
        "lean_flex_monthly": lean_flex,
        "normal_flex_monthly": normal_flex,
        "savings_monthly": savings_monthly,
        "emergency_now": round(fund_now, 2),
        "emergency_accounts": [{"name": x["name"], "balance": x["balance"]}
                               for x in emergency_accounts(f)],
        "emergency_target": emerg.get("target"),
        "emergency_gap": round(gap, 2),
        "emergency_slice": emerg_slice,
        "tiers": tiers,
        "note": ("算法：先按你的稅率把生活費換算成「要開多少發票」，最後才扣待收款——"
                 "待收款是稅前的錢，從稅後扣會把它算大三成。"
                 "而且越拖越久的帳算得越少：晚一個月只當八成，超過三個月只當四分之一。"
                 "綠色那行是「錢都收得到」的版本，那是條件，不是計畫。"),
    }


def _paid_with_days(f: F.Facts) -> list[dict]:
    """Income rows that carry a day count, for the day-rate average. Manual income logged
    with 「拍八天」 counts; a bank deposit has no idea how long the shoot was."""
    out = []
    for t in f.txns:
        d = getattr(t, "note", None) or ""
        m = re.search(r"(\d+)\s*(?:天|days?)", d)
        if m and budget.is_income(t):
            when = budget.eff_date(t)
            out.append({"amount": t.amount, "days": int(m.group(1)),
                        "date": when.isoformat() if when else None,
                        "note": (t.merchant_desc or "")[:40]})
    return out


# ── the fortnight: one line, and what kind of tight it is ────────────
#: Which lens is binding is the period's diagnosis, and each one points at a different
#: lever. This is Momo's layer 1 — "every two week should have its own health diagnosis".
_DIAGNOSIS = {
    "計畫": ("收入不夠", "接案子——但這是慢的槓桿，今天接的錢要一個半月後才到",
             "income"),
    "水位": ("錢在路上，只是還沒到", "催已經欠你的錢，還有撐住。這不是你花太兇",
             "timing"),
    "軌跡": ("花得比自己的節奏兇", "少花一點——這是唯一今天就能動的槓桿", "spending"),
}


def dip_view(line: float, spent: float, days_left: int, days_in: int,
             lean_flex_monthly: float, binding: str = "", coverage: float = 1.0) -> dict:
    """Is this still a budget, or is it an alarm?

    Momo's screen said 「還能花 $4 · 一天 $1」 for five remaining days, and she said "wow".
    The arithmetic was right and the vocabulary was wrong. The cushion lens divides
    (可動用 − 守住的水位) across the periods it has to cover; when her spendable cash sits
    almost exactly on her emergency floor the numerator collapses to nearly nothing, and a
    correct calculation comes out the other end dressed as pocket money.

    $1 a day is not an instruction. It is the same failure as the negative allowance from
    months ago wearing a new coat: a number that cannot be obeyed reads as a verdict on
    her, which is precisely what her Law forbids.

    So below the point where the line stops covering food, the card stops quoting the line
    and says the true thing instead — this is what the rest of the period costs at the
    most frugal she has ever actually lived, this much of it comes out of the emergency
    fund, and that is a fact about timing, not about her.

    Two different things can put her here and they must not be told the same way. Either
    the line was never big enough — she is at her floor and no decision of hers produced
    that, which is the case her Law protects — or the line was enough and she spent it
    down, which is a real fact about the last two weeks and hiding it would make the card
    a liar. ``cause`` carries the difference; only the first one gets 「這不是你花太兇」.

    ``binding`` is taken so the note cannot contradict the lever printed beneath it. A
    line can be below survival *and* 軌跡 can be the tightest lens — her recent pace really
    is the constraint — and in that one combination the absolution is dropped rather than
    printed directly above 「花得比自己的節奏兇」.

    Pure on purpose: the whole crisis branch is exercisable without a low-cash database.
    """
    per_day = (lean_flex_monthly / 2 / days_in) if days_in else 0.0
    survival = round(per_day * max(0, days_left), 2)
    left = max(0.0, line - spent)
    dip = round(max(0.0, survival - left), 2)
    if dip <= 0:
        return {"mode": "normal", "cause": None, "survival_need": survival, "dip": 0.0,
                "survival_full": round(per_day * days_in
                                       * max(0.0, min(1.0, coverage)), 2),
                "survival_per_day": round(per_day, 2), "line_left": round(left, 2),
                "dip_note": ""}

    # Which cause it is has to be judged over the SAME span, and the span is the one the
    # LINE buys — not the calendar period. ``survival`` covers only the days that are left,
    # so comparing them directly called her $221 line "enough" merely because it exceeds
    # five days of food. Correcting that to a full period then over-corrected: with 起算日
    # landing on 8/11 the budget governs 5 of 15 days, so the line was never meant to buy
    # fifteen days of anything, and measuring it against them blamed the water level for a
    # line that was, barely, adequate. Same error twice, once in each direction.
    full = round(per_day * days_in * max(0.0, min(1.0, coverage)), 2)
    cause = "spent" if line >= full else "line"
    head = (f"剩 {days_left} 天，最省也要 ${survival:,.0f}（一天 ${per_day:,.0f}），"
            f"線上只剩 ${left:,.0f}，差 ${dip:,.0f}。")
    tail = (("這條線本來就低於吃飯的錢"
             + ("——不是你花太兇，是水位卡在緊急預備金的邊上，"
                if binding != "軌跡" else "，是水位卡在緊急預備金的邊上；")
             + f"剩下這幾天的 ${dip:,.0f} 會直接從那一層拿。")
            if cause == "line" else
            f"這一期的線 ${line:,.0f} 本來夠用，是已經花掉了；"
            f"剩下這幾天的 ${dip:,.0f} 會從緊急預備金拿。下一期會重新算，不用把它背過去。")
    return {"mode": "dip", "cause": cause, "survival_need": survival, "dip": dip,
            "survival_full": full,
            "survival_per_day": round(per_day, 2), "line_left": round(left, 2),
            "dip_note": head + tail}


async def fortnight(session, f: F.Facts | None = None) -> dict:
    """This half-month: the line, what kind of tight it is, and where it sits in the season.

    Graded against the LINE and nothing else. Momo's Law: "I should never punish myself
    for a shortage this month, because it happened a couple weeks ago." Ten of her last
    twelve periods had less money arrive than she spent, and five had literally none — a
    system that scores her on deposits calls that ten failures, none of which were
    decisions. Staying under the line in a month where nothing landed is a WIN, and the
    cushion moving underneath is reported as information, never as a verdict.
    """
    from . import runway as RW
    from . import season as SE
    f = f or await F.build(session)
    a = await AL.compute(session)
    today = now().date()

    binding = a.get("binding") or ""
    label, lever, kind = _DIAGNOSIS.get(binding, ("", "", ""))

    # where this period sits inside the season, so the jar layer can ask for a share
    lo, hi, dl = SE.bounds(today)
    keys = [k for k in P.series(P.key_for(lo), P.key_for(hi))]
    here = budget.current_key()
    idx = keys.index(here) + 1 if here in keys else None

    spent, line = a["spent"], a["allowance"]
    under = line - spent
    fc = await RW.forecast(session, f, lean=True)

    # What eating actually costs for the days that are left. Below this a "line" stops
    # being a budget: Momo's screen showed $4 for five days — $1 a day — which is not an
    # instruction anyone can follow. It is the same failure as the negative allowance from
    # months ago, arriving through a different door: the cushion lens is correct that she
    # is at her floor, and then reports it in the vocabulary of pocket money.
    te2 = await to_earn(session, HORIZON_MONTHS, f)
    days_in = P.days_in(a["period_key"])
    dv = dip_view(line, spent, a["days_left"], days_in, te2["lean_flex_monthly"], binding,
                  coverage=a.get("coverage", 1.0))

    return {
        "period": a["period_key"], "label": a["period_label"],
        "start": a["period_start"], "end": a["period_end"],
        "days_left": a["days_left"],
        "line": round(line, 2), "spent": round(spent, 2), "left": round(under, 2),
        "per_day_left": a.get("per_day_left"),
        # one line per day, and 本期口袋 for what a day didn't use
        "daily": a.get("daily"),
        # the crisis view, for when the line falls under what living costs
        **{k: dv[k] for k in ("mode", "survival_need", "dip", "survival_per_day")},
        "dip_cause": dv["cause"],
        "floor": round(te2["fixed_monthly"] + te2["lean_flex_monthly"], 2),
        # the verdict, and ONLY against the line
        "verdict": "under" if under >= 0 else "over",
        "binding": binding, "diagnosis": label, "lever": lever, "kind": kind,
        "lenses": a.get("lenses"),
        "shortfall": a.get("shortfall"),
        "shortfall_kind": a.get("deficit_kind") or a.get("kind"),
        "session_index": idx, "session_count": len(keys),
        "season_start": lo.isoformat(), "season_end": hi.isoformat(),
        "season_tax": ({"due": dl["due"], "label": dl["label"]} if dl else None),
        # information, not a grade
        "cushion": a["reserve_total"],
        "runway_periods": fc["runway_periods"],
        "next_money": (fc["periods"][i]["label"]
                       if (i := next((j for j, r in enumerate(fc["periods"])
                                      if r["arrive"] > 0), None)) is not None else None),
        "law": ("這一期緊不緊，跟你花得好不好是兩件事。線是照你的節奏跟水位算出來的，"
                "沒進帳的那幾期只要沒超過線，就算過關。"),
        "dip_note": dv["dip_note"],
    }


# ── Module B: the earning question, put where it can be acted on ─────
async def to_book(session, f: F.Facts | None = None) -> dict:
    """How much work she has to BOOK, for the window her bookings can still reach.

    The old 需要賺 index asked "how much do I need in the next three months" and answered
    against a window she could no longer affect — 20 days from a season's end it was
    telling her to earn 34 shoot days' worth of money in 20 days. Momo: "realistic action
    is getting booked for something one or two months from now, which would no longer get
    accounted for this season."

    So this one is keyed to the LANDING date. Its window is the season that money booked
    today will actually land in, and it carries the deadline that follows from the payment
    lag: wrap by this date or the money belongs to the season after.
    """
    from . import prefs
    from . import season as SE
    f = f or await F.build(session)
    today = now().date()
    reach = today + timedelta(days=prefs.PAY_LAG_DAYS)
    lo, hi, dl = SE.bounds(reach)
    months = ((hi - lo).days + 1) / 30.4

    # Money that has ALREADY ARRIVED inside the window counts too. Leaving it out meant
    # getting paid made the dashboard ask for MORE work: Avia's $2,850 landing took
    # 「已排定」 from $7,035 to $5,040 — the pending row vanished and the real deposit was
    # invisible — so 「還要接」 jumped from 35.9 to 41.6 shoot days on the day she got paid.
    # Momo saw it coming: "the money as they come in is gonna be more than expected, so
    # that number is gonna drop once those invoices start getting paid right?" It should,
    # and by MORE than the discount suggested, because $2,850 real beats $1,995 weighted.
    covered = landed = 0.0
    covered_items = []
    for t in f.txns:
        if not budget.is_income(t):
            continue
        d = budget.eff_date(t)
        if not d or not (lo <= d <= hi):
            continue
        landed += t.amount
        covered_items.append({"note": (t.merchant_desc or "")[:40], "amount": round(t.amount, 2),
                              "weighted": round(t.amount, 2), "confidence": 1.0,
                              "stage": "paid", "days": None, "lands": d.isoformat()})
    covered += landed

    booked_days = 0
    for p in await prefs.pending_invoices(session):
        land = prefs.landing(p)
        if land is None or not (lo <= land <= hi):
            continue
        amt = float(p.get("amount") or 0)
        conf = prefs.confidence(p, today)
        covered += amt * conf
        booked_days += int(p.get("days") or 0)
        covered_items.append({"note": p.get("note"), "amount": round(amt, 2),
                              "weighted": round(amt * conf, 2), "confidence": conf,
                              "stage": prefs.stage_of(p), "days": p.get("days"),
                              "lands": land.isoformat()})
    covered_items.sort(key=lambda x: x["lands"])

    te = await to_earn(session, HORIZON_MONTHS, f)
    rate = te["tax_rate"]
    dr = te["day_rate"]["rate"]
    wrap_by = hi - timedelta(days=prefs.PAY_LAG_DAYS)

    tiers = []
    for t in te["tiers"]:
        monthly = t["net"] / HORIZON_MONTHS          # the tier's monthly cost of living
        net = monthly * months
        gross = net / (1 - rate) if rate < 1 else net
        gap = max(0.0, gross - covered)
        tiers.append({
            "name": t["name"], "why": t["why"],
            "net": round(net, 2), "gross": round(gross, 2),
            "gap": round(gap, 2), "per_month": round(gap / months, 2) if months else 0.0,
            "work_days": round(gap / dr, 1) if dr > 0 else None,
            "covered_pct": round(min(100.0, 100 * covered / gross), 1) if gross else 0.0,
        })

    return {
        "start": lo.isoformat(), "end": hi.isoformat(), "months": round(months, 2),
        "tax": ({"due": dl["due"], "label": dl["label"]} if dl else None),
        "covered": round(covered, 2), "landed": round(landed, 2),
        "booked": round(covered - landed, 2), "booked_days": booked_days,
        "covered_items": covered_items,
        "day_rate": te["day_rate"], "tiers": tiers,
        "wrap_by": wrap_by.isoformat(),
        "days_to_book": max(0, (wrap_by - today).days),
        "note": (f"今天接的案子，錢大概 {prefs.PAY_LAG_DAYS} 天後才進來，所以這裡算的是 "
                 f"{lo.strftime('%-m/%-d')}–{hi.strftime('%-m/%-d')} 這一段。"
                 f"要讓錢趕在 {hi.strftime('%-m/%-d')} 前入帳，最晚得 "
                 f"{wrap_by.strftime('%-m/%-d')} 殺青。"),
    }


def emergency_accounts(f: F.Facts) -> list[dict]:
    """Which accounts are standing in for the emergency fund.

    Every cash account whose name says savings — Momo has two (Apple GS and Chase
    Savings), and taking only the first match reported her fund as $300 instead of
    $7,770. Returned as a list so the page can show what it counted."""
    return [a for a in f.registry.values()
            if a.get("kind") == "cash" and "saving" in (a.get("name") or "").lower()]


def _emergency_balance(f: F.Facts) -> float:
    return round(sum(float(a.get("balance") or 0) for a in emergency_accounts(f)), 2)


async def _pending(session) -> list[dict]:
    from . import prefs
    return await prefs.pending_invoices(session)


def _pending_within(items: list[dict], months: int, today: date | None = None) -> float:
    """What the next `months` may lean on: inside the horizon, and after the haircut.

    Two changes from the first version, both in the same direction. An invoice with no
    date at all used to be assumed to land inside the horizon; it now counts for nothing,
    because "they'll pay me eventually" is not a month. And a late invoice is discounted
    by how late it is (:func:`prefs.confidence`) — counting Prince in Workboots at full
    face value three months after wrap told the index she needed $0 to survive."""
    from . import prefs
    today = today or now().date()
    horizon = today + timedelta(days=int(months * 30.4))
    total = 0.0
    for p in items:
        try:
            amt = float(p.get("amount") or 0)
        except (TypeError, ValueError):
            continue
        land = prefs.landing(p)
        if land is None or land > horizon:
            continue
        total += amt * prefs.confidence(p, today)
    return round(total, 2)


# ── what the next few months are likely to bring ─────────────────────
async def projection(session, months: int = HORIZON_MONTHS,
                     f: F.Facts | None = None) -> dict:
    """Income for the coming months, as a floor and a likely case rather than one number.

    Two honest quantities, never blended:

      已排定  money with a name on it — invoices Momo is waiting on, dated to the month
              they should land, plus (for the current month) what has already arrived
      照節奏  the median month of the last twelve, i.e. what happens if she books the
              kind of work she usually books

    The likely case is ``max`` of the two, not their sum: an invoice she is already owed
    is part of a typical month, not extra on top of one. Adding them would quietly
    promise her a month she has no reason to expect.

    Undated invoices are reported separately instead of being smeared across the horizon,
    because "we'll pay you eventually" is not a month.
    """
    f = f or await F.build(session)
    perf = await income_performance(session, f)
    med = perf["median_month"]
    today = now().date()

    # months in the horizon, starting with the current one
    ms: list[str] = []
    y, m = today.year, today.month
    for _ in range(months):
        ms.append(f"{y:04d}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)

    from . import prefs
    received = {r["month"]: r["amount"] for r in perf["months"]}
    booked: dict[str, float] = defaultdict(float)
    unscheduled = 0.0
    for p in await _pending(session):
        try:
            amt = float(p.get("amount") or 0)
        except (TypeError, ValueError):
            continue
        # Bucket by the month the money should ARRIVE, not the month the job happened.
        # A shoot that wraps 9/14 is September work and October money, and this page was
        # the only place still calling it September income while the calendar and the
        # allowance both planned on mid-October.
        land = prefs.landing(p)
        if land is None:
            unscheduled += amt
            continue
        conf = prefs.confidence(p, today)
        w = land.strftime("%Y-%m")
        if w in ms:
            booked[w] += amt * conf
        elif w < ms[0]:
            booked[ms[0]] += amt * conf   # already late: it lands whenever, count it now
        # landing beyond the horizon: genuinely not this quarter's money

    out = []
    for i, k in enumerate(ms):
        b = round(booked.get(k, 0.0) + (received.get(k, 0.0) if i == 0 else 0.0), 2)
        out.append({"month": k, "booked": b, "typical": round(med, 2),
                    "likely": round(max(b, med), 2), "current": i == 0})

    # …and where the year lands if the rest of it behaves like the horizon does. Only the
    # part that has not happened yet is estimated; what is already banked is banked.
    ytd = next((y["amount"] for y in perf["years"]
                if y["year"] == f"{today.year:04d}"), 0.0)
    got_now = received.get(ms[0], 0.0)
    rest = max(0.0, out[0]["likely"] - got_now) + sum(x["likely"] for x in out[1:])
    last_h = int(ms[-1][5:7]) if ms[-1][:4] == f"{today.year:04d}" else 12
    rest += med * max(0, 12 - last_h)     # months past the horizon, at her usual pace
    year_end = {"year": f"{today.year:04d}", "so_far": round(ytd, 2),
                "estimate": round(ytd + rest, 2), "to_come": round(rest, 2)}

    te = await to_earn(session, months, f)
    hold = next((t for t in te["tiers"] if t["name"] == "持平"), None)
    return {
        "months": out,
        "year_end": year_end,
        "median_month": round(med, 2),
        "booked_total": round(sum(x["booked"] for x in out), 2),
        "likely_total": round(sum(x["likely"] for x in out), 2),
        "unscheduled": round(unscheduled, 2),
        "need_per_month": hold["per_month"] if hold else None,
        "need_label": "持平",
        "note": ("已排定＝有名字的錢（待收款，本月再加上已入帳的）。照節奏＝近 12 個月的"
                 "中位數。兩個取大的，不相加——已經欠你的錢本來就算在一般月份裡。"),
    }


# ── standing: the three walls and where they are ─────────────────────
async def standing(session, f: F.Facts | None = None) -> dict:
    f = f or await F.build(session)
    a = await AL.compute(session)
    emerg = a["emergency"]
    fund = _emergency_balance(f)
    target = emerg.get("target") or 0
    return {
        "emergency": {
            "now": round(fund, 2), "target": target,
            "accounts": [{"name": x["name"], "balance": x["balance"]}
                         for x in emergency_accounts(f)],
            "pct": round(100 * fund / target, 1) if target else None,
            "months": emerg.get("months"), "why": emerg.get("why") or [],
            "components": emerg.get("components") or {}, "pinned": emerg.get("pinned"),
        },
        "tax": {
            "should_hold": a["tax"]["should_hold"], "already_paid": a["tax"]["already_paid"],
            "outstanding": a["tax"]["outstanding"], "rate": a["tax"]["rate"],
            "next": a["tax"]["next"], "next_estimate": a["tax"]["next_estimate"],
            "next_days": a["tax"]["next_days"], "caveat": a["tax"]["caveat"],
        },
        "savings_debt": a["savings_debt"],
        "ladder": a["ladder"], "standing_rung": a["standing_rung"],
        "reserve_total": a["reserve_total"],
        "shocks": a["shock_load"],
    }


# ── the calendar page ────────────────────────────────────────────────
async def calendar_items(session, days: int = 400) -> dict:
    """Everything with a date attached, in one list: renewals, tax deadlines, and money
    Momo is waiting on. The point is to see a dry month before walking into it."""
    from . import prefs
    today = now().date()
    horizon = today + timedelta(days=days)
    out = []

    for r in await FX.calendar(session, months=max(1, days // 30)):
        try:
            d = date.fromisoformat(r["due"])
        except ValueError:
            continue
        out.append({"date": r["due"], "days": (d - today).days, "kind": "renewal",
                    "label": r["name"], "amount": -abs(r["amount"]), "cat": r.get("cat"),
                    "note": ("要自己轉，不會自動扣" if r.get("manual")
                             else "預留" if r.get("sinking") else "定期扣款")})

    for d0 in TAX.DEADLINES:
        try:
            d = date.fromisoformat(d0["due"])
        except ValueError:
            continue
        if d < today or d > horizon:
            continue
        ca = "加州 0%" if d0["ca_pct"] == 0 else f"加州 {int(d0['ca_pct'] * 100)}%"
        out.append({"date": d0["due"], "days": (d - today).days, "kind": "tax",
                    "label": f"預繳稅 {d0['label']}", "amount": None, "cat": "tax",
                    "note": f"涵蓋 {d0['covers'][0]}~{d0['covers'][1]}・{ca}"})

    for p in await prefs.pending_invoices(session):
        d = prefs.landing(p)
        if d is None:
            continue
        late = (today - d).days
        conf = prefs.confidence(p, today)
        out.append({"date": d.isoformat(), "days": (d - today).days, "kind": "income",
                    "label": (p.get("note") or "某案")[:48],
                    "amount": float(p.get("amount") or 0), "cat": None,
                    "confidence": conf, "late_days": max(0, late),
                    "stage": prefs.stage_of(p),
                    "note": (("殺青 " + str(p.get("wrapped_on"))[:10] if p.get("wrapped_on")
                              else f"{p.get('when')} 的案子")
                             + f"＋{prefs.PAY_LAG_DAYS} 天付款期"
                             + (f"・晚了 {late} 天" if late > 0 else "")
                             + f"，只當 {conf:.0%} 算")})

    out.sort(key=lambda x: x["date"])
    months: dict[str, dict] = defaultdict(lambda: {"in": 0.0, "out": 0.0})
    for it in out:
        b = months[it["date"][:7]]
        if it["amount"] is None:
            continue
        if it["amount"] >= 0:
            b["in"] += it["amount"]
        else:
            b["out"] += -it["amount"]
    return {
        "items": out,
        "by_month": [{"month": k, "in": round(v["in"], 2), "out": round(v["out"], 2),
                      "net": round(v["in"] - v["out"], 2)}
                     for k, v in sorted(months.items())],
        "today": today.isoformat(),
    }

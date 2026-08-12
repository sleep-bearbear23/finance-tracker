"""How many periods until the money runs out, and which of them work can still fix.

Momo: "how would I answer the question 'how much do I need to earn the next three
months?'" — after we had already established that payment lands ~45 days after wrap, so
work booked today cannot reach the near periods at all.

The old answer was one aggregate number over a fixed window, and it was unusable twice
over: it asked for 34 shoot days inside 20 remaining days, and it could not distinguish
a shortfall she could still work her way out of from one where the lag had already closed
the door.

So the answer is a timeline, not a number. Cash is projected forward period by period with
what is owed landing when it is expected. Wherever it goes under, the module works
backwards through the payment lag to the last day a shoot could have wrapped and still
covered it — and if that day has passed, it says so, because telling her to hustle for a
period she physically cannot reach is the exact thing her Law forbids:

    "I should never punish myself for a shortage this month, because it happened a
     couple weeks ago."

Two levers, and they operate on different clocks. Spending changes tomorrow. Earning is
book-now-earn-later. This module is what makes that asymmetry visible instead of merely
true.
"""
from __future__ import annotations

from datetime import date, timedelta

from . import allowance as AL
from . import budget
from . import facts as F
from . import period as P
from . import prefs
from .config import now

#: How far to look. Eight half-months is a third of a year — far enough that a booking
#: made today lands inside it, short enough that the far end is not fantasy.
HORIZON_PERIODS = 8


async def forecast(session, f: F.Facts | None = None, periods: int = HORIZON_PERIODS,
                   lean: bool = True) -> dict:
    """Project cash forward, period by period.

    ``lean`` picks which burn rate to run: her survival floor, or how she actually lives.
    Both are worth seeing — the gap between them is the size of the lever she controls.
    """
    f = f or await F.build(session)
    from . import analytics as AN

    a = await AL.compute(session)
    te = await AN.to_earn(session, 3, f)
    today = now().date()

    fixed_p = te["fixed_monthly"] / 2
    flex_p = (te["lean_flex_monthly"] if lean else te["normal_flex_monthly"]) / 2
    burn = round(fixed_p + flex_p, 2)

    # 可動用: cash, less card debt, less the tax already earmarked. Going below zero here
    # does not mean destitute — it means the tax money is being eaten, which is its own
    # kind of emergency and should be named as one.
    bal = a["reserve_total"]
    pend = await prefs.pending_invoices(session)

    rows, key = [], budget.current_key()
    for i in range(periods):
        if i:
            key = P.next_key(key)
        lo, hi = P.key_bounds(key)
        arrive, items = 0.0, []
        for p in pend:
            land = prefs.landing(p)
            if land is None or not (lo <= land <= hi):
                continue
            amt = float(p.get("amount") or 0) * prefs.confidence(p, today)
            arrive += amt
            items.append({"note": p.get("note"), "amount": round(amt, 2),
                          "face": round(float(p.get("amount") or 0), 2),
                          "lands": land.isoformat(), "stage": prefs.stage_of(p)})
        opening = bal
        bal = round(bal + arrive - burn, 2)
        # the last day a shoot could wrap and still have the money by the time this period
        # starts. Past that, no amount of new work reaches this period.
        wrap_by = lo - timedelta(days=prefs.PAY_LAG_DAYS)
        rows.append({
            "key": key, "label": P.label(key),
            "start": lo.isoformat(), "end": hi.isoformat(),
            "arrive": round(arrive, 2), "items": items,
            "burn": burn, "opening": round(opening, 2), "closing": bal,
            "short": round(max(0.0, -bal), 2),
            "state": "broke" if bal < 0 else ("thin" if bal < burn else "ok"),
            "wrap_by": wrap_by.isoformat(),
            "bookable": wrap_by > today,     # can new work still reach this period?
            "current": i == 0,
        })

    first = next((r for r in rows if r["state"] == "broke"), None)
    thin = next((r for r in rows if r["state"] != "ok"), None)
    return {
        "burn": burn, "lean": lean, "fixed_period": round(fixed_p, 2),
        "flex_period": round(flex_p, 2),
        "start_cash": a["reserve_total"],
        "periods": rows,
        "first_broke": first, "first_thin": thin,
        "runway_periods": (rows.index(first) if first else None),
        "note": ("水位＝現金扣掉卡債、再扣掉已經預留的稅。跌破零不是身無分文，"
                 "是開始吃到繳稅的錢——那是另一種急。"),
    }


async def plan(session, f: F.Facts | None = None) -> dict:
    """The earning goal, as a schedule with deadlines instead of a lump sum.

    Every period that goes under gets three facts: how much is missing, the last day a
    shoot could wrap and still cover it, and whether that day is behind us. The last one
    is the important one — where booking cannot reach, the honest advice is chasing what
    is already owed and holding the line, and saying "go find work" would be noise."""
    f = f or await F.build(session)
    from . import analytics as AN

    fc = await forecast(session, f, lean=True)
    fc_normal = await forecast(session, f, lean=False)
    te = await AN.to_earn(session, 3, f)
    rate = te["day_rate"]["rate"]
    today = now().date()

    gaps = []
    running = 0.0
    for r in fc["periods"]:
        if r["short"] <= 0:
            continue
        need = round(r["short"] - running, 2)      # only the NEW shortfall this period
        if need <= 0:
            continue
        running += need
        gross = need / (1 - te["tax_rate"]) if te["tax_rate"] < 1 else need
        gaps.append({
            "key": r["key"], "label": r["label"], "start": r["start"],
            "need_net": need, "need_gross": round(gross, 2),
            "work_days": round(gross / rate, 1) if rate > 0 else None,
            "wrap_by": r["wrap_by"], "bookable": r["bookable"],
            "days_to_wrap": (date.fromisoformat(r["wrap_by"]) - today).days,
        })

    owed = [{"note": p.get("note"), "amount": round(float(p.get("amount") or 0), 2),
             "stage": prefs.stage_of(p),
             "lands": (l.isoformat() if (l := prefs.landing(p)) else None),
             "late_days": max(0, (today - l).days) if l else 0}
            for p in await prefs.pending_invoices(session)]
    owed.sort(key=lambda x: -x["late_days"])

    unreachable = [g for g in gaps if not g["bookable"]]
    reachable = [g for g in gaps if g["bookable"]]
    return {
        "lean": fc, "normal": fc_normal,
        "gaps": gaps, "reachable": reachable, "unreachable": unreachable,
        "day_rate": te["day_rate"],
        "owed": owed,
        "owed_total": round(sum(o["amount"] for o in owed), 2),
        "levers": _levers(fc, unreachable, reachable),
    }


def _levers(fc: dict, unreachable: list, reachable: list) -> list[dict]:
    """What can actually be done, in the order it can be done."""
    out = []
    if unreachable:
        first = unreachable[0]
        out.append({
            "kind": "chase",
            "when": first["label"],
            "text": (f"{first['label']} 會見底，缺 ${first['need_net']:,.0f}。"
                     f"要靠新案子補，最晚得 {first['wrap_by']} 殺青——那天已經過了，"
                     "所以這一期接再多案子都趕不上。能動的只有兩件事："
                     "催已經欠你的錢，還有把花費壓在最省那條線。"),
        })
    if reachable:
        g = reachable[0]
        out.append({
            "kind": "book",
            "when": g["label"],
            "text": (f"{g['label']} 缺 ${g['need_gross']:,.0f}（稅前）"
                     + (f"，大約 {g['work_days']} 個拍攝日" if g["work_days"] else "")
                     + f"。最晚 {g['wrap_by']} 殺青，從今天算還有 {g['days_to_wrap']} 天可以排。"),
        })
    if not fc["first_thin"]:
        out.append({"kind": "clear", "when": None,
                    "text": "接下來八期都撐得住，沒有非接不可的案子。"})
    return out

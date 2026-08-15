"""The earning-side watch loop — the half 陳會計 never had.

Everything she did unprompted was about the spending lever: what did you buy, the 80%
alert, the 9pm reminder. But Momo's spending moves by hundreds a fortnight and her income
moves by thousands, and spending alerts are structurally late — a bad month's cause was
the calls that didn't come 6–8 weeks earlier. Booking is the only lever that can be
warned about in time. This module is her noticing, early, out loud.

Three signals (the closure test lives with the annual layer):

  thin pipeline   next month's booked shoot days vs a baseline. First year there is no
                  「去年這時候」 — the record starts 2026-01 — so the baseline is the
                  trailing median, floored at survival days DERIVED from the engine:
                  (fixed + lean flex) ÷ net day rate. When rent or the rate moves, the
                  floor moves with it; nothing goes stale.
  aging money     an invoice past its expected landing + 7 days; a claim at todo > 10d
                  or sent > 30d. First mention when it crosses, then folded into the
                  weekly check while open. Stops the moment it is paid or eaten.
  rate drift      noted at booking time, in her confirmation, because that is the only
                  moment the information is actionable. One line, no question.

Every message is assisted judgement: situation, both levers priced, no verdict.
"""
from __future__ import annotations

import statistics
from datetime import date

from . import claims as CL
from . import prefs
from .config import now
from .db import get_kv, set_kv

#: an invoice is worth chasing this many days after its expected landing
INVOICE_GRACE_DAYS = 7
#: a booking this far under her recent rate gets a note in the receipt
RATE_DRIFT = 0.8
#: months of history the trailing median looks at
BASELINE_WINDOW = 6


async def _net_rate(session) -> float:
    """Her recent contracted day rate, after the tax hold."""
    from . import seed_invoices as SI
    from . import tax as TAX
    invs = [r for r in await SI.invoices(session)
            if r.get("kind", "shoot") == "shoot" and r.get("rate")]
    recent = [float(r["rate"]) for r in invs[-3:]]
    rate = statistics.median(recent) if recent else 0.0
    keep = 1.0 - (await TAX.status(session, now().date()))["rate"]
    return rate * max(0.0, keep)


async def survival_days(session) -> dict:
    """How many shoot days a month just to exist — the floor under the baseline."""
    from . import analytics as AN
    from . import facts as F
    te = await AN.to_earn(session, 3)
    monthly = te["fixed_monthly"] + te["lean_flex_monthly"]
    net = await _net_rate(session)
    return {"days": round(monthly / net, 1) if net else None,
            "monthly": round(monthly, 2), "net_rate": round(net, 2)}


async def _booked_days_by_month(session) -> dict[str, float]:
    """Shoot days per work-month, archive + pending. Jobs with no day count are unknown,
    not zero — Rule 2 — so they are simply absent and the caller says so."""
    from . import seed_invoices as SI
    out: dict[str, float] = {}
    for r in await SI.invoices(session):
        ym, d = str(r.get("when") or r.get("date") or "")[:7], r.get("days")
        if ym and d:
            out[ym] = out.get(ym, 0.0) + float(d)
    import re
    for p in await prefs.pending_invoices(session):
        ym, d = str(p.get("when") or "")[:7], p.get("days")
        if not d:
            # her own words carry the count — 「拍八天」「拍8天」 — and a day count sitting
            # in prose is exactly how the wrap dates got lost (I-3). Read it.
            m = re.search(r"拍\s*([0-9一二三四五六七八九十]+)\s*天", p.get("note") or "")
            if m:
                zh = dict(zip("一二三四五六七八九十", range(1, 11)))
                d = zh.get(m.group(1)) or (int(m.group(1)) if m.group(1).isdigit() else None)
        if ym and d:
            out[ym] = out.get(ym, 0.0) + float(d)
    return out


async def pipeline(session) -> dict:
    """Next month's booked days against the baseline."""
    today = now().date()
    nxt = date(today.year + (today.month == 12), today.month % 12 + 1, 1)
    ym = nxt.strftime("%Y-%m")
    by_month = await _booked_days_by_month(session)
    booked = by_month.get(ym, 0.0)

    hist = sorted(by_month)
    trail = [by_month[k] for k in hist if k < today.strftime("%Y-%m")][-BASELINE_WINDOW:]
    med = statistics.median(trail) if trail else 0.0
    sv = await survival_days(session)
    floor = sv["days"] or 0.0
    baseline = max(med, floor)
    return {"month": ym, "booked": booked, "median": round(med, 1),
            "floor": floor, "baseline": round(baseline, 1),
            "thin": baseline > 0 and booked < baseline,
            "gap_days": round(max(0.0, baseline - booked), 1),
            "known_months": len(trail), "survival": sv}


async def chase_list(session) -> list[str]:
    """Everything past its line, worded for the weekly check."""
    today = now().date()
    lines = []
    for p in await prefs.pending_invoices(session):
        land = prefs.landing(p)
        if land and (today - land).days > INVOICE_GRACE_DAYS:
            lines.append(f"「{(p.get('note') or '')[:24]}」${float(p.get('amount') or 0):,.0f} "
                         f"超過預計入帳 {(today - land).days} 天了，催一下")
    for c in await CL.outstanding(session):
        if c.get("nudge"):
            who = "還沒去要" if c["state"] == "todo" else "要了他們還沒還"
            lines.append(f"{c['merchant']} ${c['amount']:,.2f} {who}，"
                         f"放 {c['age']} 天了")
    return lines


async def rate_note(session, amount: float, days) -> str | None:
    """One line in the booking receipt when the rate is well under her recent price.

    Information at the decision moment; the data is stored untouched and nothing asks
    her to justify it — she knew what she agreed to."""
    try:
        d = float(days or 0)
        rate = float(amount) / d if d else 0.0
    except (TypeError, ValueError):
        return None
    if rate <= 0:
        return None
    from . import seed_invoices as SI
    invs = [r for r in await SI.invoices(session)
            if r.get("kind", "shoot") == "shoot" and r.get("rate")]
    recent = [float(r["rate"]) for r in invs[-3:]]
    if not recent:
        return None
    med = statistics.median(recent)
    if med > 0 and rate < med * RATE_DRIFT:
        return (f"這單算下來 ${rate:,.0f}／天，你最近三個是 ${med:,.0f}——知道就好。")
    return None


async def weekly(session) -> str | None:
    """The Monday look-ahead. Says nothing when there is nothing to say."""
    key = f"watch:weekly:{now().date().isocalendar().year}-{now().date().isocalendar().week}"
    if await get_kv(session, key):
        return None
    pipe = await pipeline(session)
    chases = await chase_list(session)
    if not pipe["thin"] and not chases:
        return None                      # a quiet pipeline in a booked month sends nothing

    bits = []
    if pipe["thin"]:
        base_why = (f"你最近幾個月中位數是 {pipe['median']:.0f} 天"
                    if pipe["median"] >= pipe["floor"] and pipe["known_months"] >= 3
                    else f"光是過日子就要 {pipe['floor']:.0f} 天")
        sv = pipe["survival"]
        lever_earn = f"還要接 {pipe['gap_days']:.0f} 天"
        bits.append(f"{pipe['month'][5:].lstrip('0')}月目前排 {pipe['booked']:.0f} 天，"
                    f"{base_why}。{lever_earn}，或者花費那邊自己抓——你自己看。")
        if pipe["known_months"] < 3:
            bits.append("（我手上有記天數的月份還不多，這個基準先看看就好。）")
    bits.extend(chases)
    await set_kv(session, key, "1")
    return "\n".join(bits) if bits else None

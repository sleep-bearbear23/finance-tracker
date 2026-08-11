"""The numbers the detail pages are built on — one module so no page invents its own.

The overview was carrying five jobs at once. These are the computations the separate
pages need, all derived from :mod:`app.facts` and the same budget helpers, so a figure on
the 計畫 page can never disagree with the same figure on the overview.

The centrepiece is :func:`to_earn` — "how much more do I need to make in the next three
months". Momo asked for it as an index she can hold up against a gig offer, which means
it has to be a small number of dollars and a count of jobs, not a spreadsheet.
"""
from __future__ import annotations

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
        payers[(t.merchant_desc or "?")[:40]] += t.amount
        rows.append({"date": d.isoformat(), "name": t.merchant_desc,
                     "amount": round(t.amount, 2), "source": t.source, "note": t.note})
    rows.sort(key=lambda r: r["date"], reverse=True)

    hkeys = P.last_n(budget.current_key(), 12)
    mkeys = sorted(months)[-12:]
    mvals = [months[k] for k in mkeys]

    return {
        "halves": [{"key": k, "label": P.label(k), "month_start": P.is_month_start(k),
                    "amount": round(halves.get(k, 0.0), 2)} for k in hkeys],
        "months": [{"month": k, "amount": round(months[k], 2)} for k in mkeys],
        "years": [{"year": k, "amount": round(v, 2)} for k, v in sorted(years.items())],
        "payers": [{"name": k, "amount": round(v, 2)}
                   for k, v in sorted(payers.items(), key=lambda x: -x[1])[:12]],
        "rows": rows,
        "median_month": round(statistics.median(mvals), 2) if mvals else 0.0,
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

    # what living actually costs, per month, from her own record
    lean_flex = AL.LEAN_FLEX_MONTHLY
    normal_flex = round((a["recent_median"] or 0) * 2, 2) or lean_flex
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

    pending = _pending_within(await _pending(session), months)

    rate = tax_st["rate"]

    def tier(name: str, net_monthly: float, extra: float, why: str) -> dict:
        net = net_monthly * months + extra
        gross = net / (1 - rate) if rate < 1 else net
        need = max(0.0, gross - pending)
        return {"name": name, "net": round(net, 2), "gross": round(gross, 2),
                "need": round(need, 2), "per_month": round(need / months, 2), "why": why}

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
    med = perf["median_payment"] or 0.0
    for t in tiers:
        t["gigs"] = round(t["need"] / med, 1) if med > 0 else None

    return {
        "months": months,
        "tax_rate": rate,
        "pending": round(pending, 2),
        "median_payment": med,
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
        "note": ("已經做完但還沒收到的錢會先扣掉——那些本來就會進來，"
                 "不用再靠接新案子。"),
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
    today = today or now().date()
    horizon = today + timedelta(days=int(months * 30.4))
    total = 0.0
    for p in items:
        w = str(p.get("when") or "")[:7]
        try:
            amt = float(p.get("amount") or 0)
        except (TypeError, ValueError):
            continue
        if not w:
            total += amt          # no date given: assume it lands inside the horizon
            continue
        try:
            y, m = int(w[:4]), int(w[5:7])
        except ValueError:
            continue
        landing = date(y + (m // 12), (m % 12) + 1, 1)
        if landing <= horizon:
            total += amt
    return total


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
                    "label": r["name"], "amount": -abs(r["amount"]),
                    "cat": r.get("cat"), "note": "定期扣款" if not r.get("sinking") else "預留"})

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
        w = str(p.get("when") or "")[:7]
        try:
            y, m = int(w[:4]), int(w[5:7])
            d = date(y + (m // 12), (m % 12) + 1, 1) + timedelta(days=13)
        except (ValueError, IndexError):
            continue
        out.append({"date": d.isoformat(), "days": (d - today).days, "kind": "income",
                    "label": (p.get("note") or "某案")[:48],
                    "amount": float(p.get("amount") or 0), "cat": None,
                    "note": "預估到帳（含 14 天寬限）" + ("・已逾期" if d < today else "")})

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

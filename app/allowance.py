"""The allowance — three lenses, the most conservative one wins, and she says which.

A single formula would be easy and wrong. Income is lumpy, the plan is a guess, and the
bank balance knows things the plan doesn't. So the number is computed three ways:

  計畫  plan        income − tax − fixed − savings.  What the month is supposed to look like.
  水位  cushion     how much can leave the pile before it drops through the next rung.
  軌跡  trajectory  what Momo has actually been spending, corrected by which way net worth
                    is moving. Catches "the plan says fine" while the balance drains.

The smallest wins, and :func:`compute` always reports *which* one bound and why. A number
Momo can't interrogate is a number she won't trust, and she's right not to.

Two rules that never bend:

* Expected income is a clock, not credit. A booked gig can only ever tell her how long she
  has to self-fund; it can never raise the allowance. See :func:`_cushion`.
* Nothing here silently rounds a deficit up to zero. "$0 to spend" and "you're $600 short
  this period" are different sentences and only one of them is true.
"""
from __future__ import annotations

import json
from datetime import date, timedelta

from . import budget
from . import fixed as FX
from . import networth
from . import period as P
from . import prefs
from . import tax as TAX
from .config import now
from .db import get_kv, set_kv

# ── the ladder ───────────────────────────────────────────────────────
#: rungs are months of *survival* burn — fixed costs plus a lean-but-livable flex floor,
#: taken from Momo's own cheapest months (食 bottomed at $474, snacks at $13, 娛樂 at $0).
LEAN_FLEX_MONTHLY = 550.0
RUNGS = [("第一階", 1), ("第二階", 2), ("第三階", 3)]
EMERGENCY_TARGET_DEFAULT = 10000.0

#: a lean period should still be livable — the allowance is never *recommended* below
#: this share of the plan figure, even when cushion or trajectory says lower.
GENTLE_FLOOR = 0.50

#: how much of income the amortised shocks may eat before she stops smoothing and says
#: the truth: you're not absorbing these, you're deferring them.
SHOCK_LOAD_CAP = 0.15

START_KEY = "cfg_budget_from"      # 起算日 — before this date, spending is recorded not judged
SHOCKS_KEY = "cfg_shocks"
SAVDEBT_KEY = "cfg_savings_debt"
EMERG_KEY = "cfg_emergency_target"


# ── 起算日 ────────────────────────────────────────────────────────────
async def start_date(session) -> date | None:
    raw = await get_kv(session, START_KEY)
    if not raw:
        return None
    try:
        return date.fromisoformat(raw[:10])
    except ValueError:
        return None


async def set_start_date(session, d: date | str) -> str:
    d = d if isinstance(d, str) else d.isoformat()
    await set_kv(session, START_KEY, d[:10])
    return d[:10]


def coverage(key: str, begin: date | None) -> tuple[float, date]:
    """What fraction of this half-month the budget actually governs, and from when.

    The system can't go live on the 1st of a period, and it shouldn't pretend it did.
    Everything Momo spent before 起算日 is recorded and shown, and charged to nobody."""
    lo, hi = P.key_bounds(key)
    if not begin or begin <= lo:
        return 1.0, lo
    if begin > hi:
        return 0.0, hi
    days = (hi - begin).days + 1
    return days / P.days_in(key), begin


# ── shocks: 自己造成 vs 無法避免 ───────────────────────────────────────
SELF = "self"            # a ticket, a late fee, an impulse blowout → repaid from allowance
UNAVOIDABLE = "unavoidable"   # health, a real emergency → draws cushion, no repayment


async def shocks(session) -> list[dict]:
    try:
        got = json.loads(await get_kv(session, SHOCKS_KEY) or "[]")
    except (TypeError, ValueError):
        got = []
    return got if isinstance(got, list) else []


async def add_shock(session, amount: float, kind: str, note: str = "",
                    periods: int = 4, when: str | None = None) -> dict:
    """Record a one-off hit. 自己造成 spreads over `periods` half-months so a $400 ticket
    dents the daily number instead of erasing two weeks."""
    items = await shocks(session)
    item = {
        "amount": round(float(amount), 2), "kind": kind, "note": note,
        "when": when or now().date().isoformat(),
        "periods": max(1, int(periods)) if kind == SELF else 0,
        "repaid": 0.0,
    }
    items.append(item)
    await set_kv(session, SHOCKS_KEY, json.dumps(items, ensure_ascii=False))
    return item


async def shock_load(session, income_period: float) -> dict:
    """Per-period repayment for self-inflicted shocks, plus an honest flag when the
    smoothing has stopped being smoothing."""
    per = 0.0
    live = []
    for s in await shocks(session):
        if s.get("kind") != SELF:
            continue
        owed = round(float(s.get("amount") or 0) - float(s.get("repaid") or 0), 2)
        if owed <= 0.01:
            continue
        slice_ = round(owed / max(1, int(s.get("periods") or 1)), 2)
        per += slice_
        live.append({**s, "outstanding": owed, "per_period": slice_})
    cap = round(income_period * SHOCK_LOAD_CAP, 2) if income_period > 0 else 0.0
    return {"per_period": round(per, 2), "items": live, "cap": cap,
            "over_cap": bool(cap and per > cap)}


# ── 存錢欠帳 ──────────────────────────────────────────────────────────
async def savings_debt(session) -> float:
    try:
        return round(float(await get_kv(session, SAVDEBT_KEY) or 0.0), 2)
    except (TypeError, ValueError):
        return 0.0


async def add_savings_debt(session, amount: float) -> float:
    total = round(await savings_debt(session) + float(amount), 2)
    await set_kv(session, SAVDEBT_KEY, str(total))
    return total


async def pay_savings_debt(session, amount: float) -> float:
    total = round(max(0.0, await savings_debt(session) - float(amount)), 2)
    await set_kv(session, SAVDEBT_KEY, str(total))
    return total


# ── the pile, and how far it is from the floor ───────────────────────
async def emergency_target(session) -> float:
    try:
        return float(await get_kv(session, EMERG_KEY) or EMERGENCY_TARGET_DEFAULT)
    except (TypeError, ValueError):
        return EMERGENCY_TARGET_DEFAULT


def ladder(survival_monthly: float, target: float) -> list[dict]:
    out = [{"name": n, "months": m, "amount": round(survival_monthly * m, 2)}
           for n, m in RUNGS]
    out.append({"name": "目標", "months": round(target / survival_monthly, 1)
                if survival_monthly else None, "amount": round(target, 2)})
    return out


def rung_below(rungs: list[dict], amount: float) -> dict | None:
    """The highest rung the pile currently clears — the floor we defend this period."""
    cleared = [r for r in rungs if amount >= r["amount"]]
    return cleared[-1] if cleared else None


# ── the lenses ───────────────────────────────────────────────────────
def plan_income(expected: float, actual: float) -> tuple[float, str]:
    """The income figure the plan lens is allowed to use — an asymmetric ratchet.

    Momo's rule, verbatim: expected income "would never be used to get her to feel like I
    could spend more, but it could act as a heads up about an upcoming dry month." So a
    forecast that is LOWER than recent reality pulls the plan down (a dry month is coming),
    and a forecast that is higher is ignored entirely (a booked gig is not money).

    Without this, booking a $9,000 job in December would quietly raise today's allowance,
    which is exactly the failure the whole design exists to prevent."""
    if expected > 0 and actual > 0:
        if expected < actual:
            return expected, f"預估收入 ${expected:,.0f} 比最近實收 ${actual:,.0f} 低，用低的抓"
        return actual, f"實際收到的 ${actual:,.0f}（預估更高，但錢還沒進來就不算）"
    if actual > 0:
        return actual, f"實際收到的 ${actual:,.0f}"
    if expected > 0:
        return expected, f"還沒有實收紀錄，先用預估 ${expected:,.0f}"
    return 0.0, "最近沒有收入紀錄"


def _plan(income_after_tax: float, fixed_p: float, savings_p: float) -> dict:
    val = income_after_tax - fixed_p - savings_p
    return {"name": "計畫", "value": round(val, 2),
            "why": (f"扣完稅的收入 ${income_after_tax:,.0f} − 固定 ${fixed_p:,.0f} "
                    f"− 存錢 ${savings_p:,.0f}")}


def _cushion(free: float, floor: float, periods_to_money: int) -> dict:
    """Expected income enters here and ONLY here — as `periods_to_money`, the number of
    half-months the pile has to stretch. A bigger booked gig makes the wait shorter; it
    never makes the pile bigger."""
    n = max(1, periods_to_money)
    val = (free - floor) / n
    return {"name": "水位", "value": round(val, 2),
            "why": (f"（可動用 ${free:,.0f} − 守住的水位 ${floor:,.0f}）÷ 撐 {n} 期")}


def _trajectory(recent_median: float, drift_per_period: float) -> dict:
    """What she's really been spending, pulled down when net worth is sliding."""
    val = recent_median + min(0.0, drift_per_period)
    why = f"最近半個月中位數 ${recent_median:,.0f}"
    if drift_per_period < 0:
        why += f"，但淨值每期掉 ${abs(drift_per_period):,.0f}，往下修"
    return {"name": "軌跡", "value": round(val, 2), "why": why}


async def _recent_discretionary(session, key: str, n: int = 6) -> tuple[float, list]:
    """Median discretionary spend over the trailing window, normalised per day so a
    partial period (the first one, or a trip) can't read as a cheap month."""
    from sqlalchemy import select

    from .models import Transaction
    keys = P.last_n(P.prev_key(key), n)
    lo, _ = P.key_bounds(keys[0])
    _, hi = P.key_bounds(keys[-1])
    rows = (await session.execute(select(Transaction))).scalars().all()
    per = {k: 0.0 for k in keys}
    for t in rows:
        if not budget.is_discretionary(t):
            continue
        d = budget.eff_date(t)
        if not d or d < lo or d > hi:
            continue
        k = P.key_for(d)
        if k in per:
            per[k] += budget.spend_amount(t)
    begin = await start_date(session)
    vals = []
    for k in keys:
        frac, _ = coverage(k, begin)
        if frac <= 0:
            continue
        vals.append(per[k] / frac if frac < 1 else per[k])   # normalise partial periods
    if not vals:
        return 0.0, []
    vals_sorted = sorted(vals)
    mid = len(vals_sorted) // 2
    med = (vals_sorted[mid] if len(vals_sorted) % 2
           else (vals_sorted[mid - 1] + vals_sorted[mid]) / 2)
    return round(med, 2), [{"key": k, "spend": round(per[k], 2)} for k in keys]


async def _net_drift(session, n: int = 6) -> float:
    """Net-worth change per half-month over recent snapshots. Negative = draining."""
    from sqlalchemy import select

    from .models import Snapshot
    snaps = (await session.execute(select(Snapshot).order_by(Snapshot.day))).scalars().all()
    if len(snaps) < 2:
        return 0.0
    first, last = snaps[0], snaps[-1]
    try:
        d0, d1 = date.fromisoformat(first.day), date.fromisoformat(last.day)
    except ValueError:
        return 0.0
    days = (d1 - d0).days
    if days < 7:
        return 0.0
    per_day = (last.net_worth - first.net_worth) / days
    return round(per_day * 15.2, 2)   # one half-month


async def _periods_to_money(session, today: date) -> tuple[int, dict | None]:
    """How many half-months until the next payment is expected to land.

    Padded by 14 days because productions are late — Momo asked for that explicitly, and
    the padding only ever makes the cushion lens tighter."""
    pend = await prefs.pending_invoices(session)
    best, item = None, None
    for p in pend:
        w = str(p.get("when") or "")[:7]
        if not w:
            continue
        try:
            y, m = int(w[:4]), int(w[5:7])
        except ValueError:
            continue
        # a month-granularity estimate means end of month, then +14 days of padding
        landing = date(y + (m // 12), (m % 12) + 1, 1) + timedelta(days=13)
        if landing < today:
            continue
        if best is None or landing < best:
            best, item = landing, p
    if best is None:
        return 4, None      # nothing booked: assume two months of self-funding
    span = max(1, round((best - today).days / 15.2))
    return int(span), {"note": item.get("note"), "amount": item.get("amount"),
                       "when": item.get("when"), "landing": best.isoformat()}


# ── the answer ───────────────────────────────────────────────────────
async def compute(session, key: str | None = None) -> dict:
    key = key or budget.current_key()
    today = now().date()
    lo, hi = P.key_bounds(key)
    begin = await start_date(session)
    frac, from_day = coverage(key, begin)

    inc = await budget.income_basis(session, key)
    tax_st = await TAX.status(session, today)
    fixed_monthly = await FX.monthly_total(session)
    prefs_ = await prefs.get_prefs(session)

    # NOT inc["used"] — that blend lets a booked gig lift the allowance. See plan_income.
    income_p, income_why = plan_income(inc["expected"], inc["actual"])
    income_after_tax = income_p * (1 - tax_st["rate"])
    fixed_p = P.split_monthly(fixed_monthly, key) * frac
    savings_full = budget.savings_for(prefs_["savings_amount"], prefs_["savings_cadence"],
                                      key, income_p)
    savings_p = savings_full * frac

    # the pile
    nw = await networth.compute(session)
    emerg_target = await emergency_target(session)
    survival_monthly = fixed_monthly + LEAN_FLEX_MONTHLY
    rungs = ladder(survival_monthly, emerg_target)

    # Cash Momo could actually deploy: liquid, minus the card she owes, minus tax that
    # isn't hers. The emergency fund stays IN — the ladder is what protects it.
    reserve_total = round(nw["spendable"] - nw["debts"] - tax_st["outstanding"], 2)
    standing = rung_below(rungs, reserve_total)
    floor = standing["amount"] if standing else 0.0

    periods_out, next_money = await _periods_to_money(session, today)

    recent_med, recent_rows = await _recent_discretionary(session, key)
    drift = await _net_drift(session)

    # Savings is SOFT: in a lean period it gets skipped before spending gets cut, and the
    # skipped amount is remembered as 存錢欠帳 rather than quietly forgotten. Compute is
    # read-only, so the IOU is reported here and committed at period close.
    savings_skipped = 0.0
    if income_after_tax - fixed_p - savings_p < 0 < savings_p:
        savings_skipped = round(min(savings_p, savings_p - (income_after_tax - fixed_p)), 2)
        savings_skipped = min(savings_p, max(0.0, savings_skipped))
        savings_p = round(savings_p - savings_skipped, 2)

    lenses = [
        _plan(income_after_tax, fixed_p, savings_p),
        _cushion(reserve_total, floor, periods_out),
        _trajectory(recent_med * frac, drift * frac),
    ]
    binding = min(lenses, key=lambda x: x["value"])
    raw = binding["value"]

    # a lean period should still be livable — but the floor can never LIFT a plan deficit
    plan_val = lenses[0]["value"]
    gentle = plan_val * GENTLE_FLOOR if plan_val > 0 else plan_val
    recommended = max(raw, gentle) if raw < gentle else raw

    # shocks Momo owes herself
    load = await shock_load(session, income_p)
    recommended -= load["per_period"]

    spent = await _spent(session, key, from_day, hi)
    remaining = round(recommended - spent, 2)
    days_left = max(0, (hi - today).days + 1) if lo <= today <= hi else 0

    deficit = plan_val < 0
    kind = None
    if deficit:
        # timing-short (money is coming) vs structurally-short (it isn't)
        pipeline = sum(float(p.get("amount") or 0)
                       for p in await prefs.pending_invoices(session))
        kind = "timing" if pipeline >= abs(plan_val) else "structural"

    return {
        "period_key": key, "period_label": P.label(key),
        "period_start": lo.isoformat(), "period_end": hi.isoformat(),
        "budget_from": from_day.isoformat(), "coverage": round(frac, 3),
        "partial": frac < 1.0,
        "days_left": days_left, "days_in_period": P.days_in(key),

        "income_period": round(income_p, 2),
        "income_basis_why": income_why,
        "income_expected": round(inc["expected"], 2),
        "income_actual": round(inc["actual"], 2),
        "income_after_tax": round(income_after_tax, 2),
        "tax": tax_st,
        "fixed_monthly": fixed_monthly, "fixed_period": round(fixed_p, 2),
        "savings_period": round(savings_p, 2),
        "savings_skipped": savings_skipped,
        "savings_debt": await savings_debt(session),

        "lenses": lenses,
        "binding": binding["name"],
        "binding_why": binding["why"],
        "raw_allowance": round(raw, 2),
        "allowance": round(recommended, 2),
        "gentle_floor_applied": bool(raw < gentle and plan_val > 0),

        "spent": spent,
        "remaining": remaining,
        "per_day_left": round(max(0.0, remaining) / days_left, 2) if days_left else None,
        "pct_used": round(100 * spent / recommended, 1) if recommended > 0 else None,

        "reserve_total": reserve_total,
        "standing_rung": standing,
        "ladder": rungs,
        "emergency_target": emerg_target,
        "periods_to_money": periods_out,
        "next_money": next_money,

        "shock_load": load,
        "recent_median": recent_med,
        "recent": recent_rows,
        "net_drift_per_period": drift,

        "deficit": deficit,
        "deficit_kind": kind,
    }


async def _spent(session, key: str, from_day: date, hi: date) -> float:
    """Discretionary spend inside the governed part of the period only."""
    from sqlalchemy import select

    from .models import Transaction
    rows = (await session.execute(select(Transaction))).scalars().all()
    return round(sum(budget.spend_amount(t) for t in rows
                     if budget.is_discretionary(t)
                     and (d := budget.eff_date(t)) and from_day <= d <= hi), 2)


def explain(a: dict) -> list[str]:
    """Her actual reasoning, in order, in words. If she can't say it she shouldn't use it."""
    out = []
    if a["partial"]:
        out.append(f"這期我從 {a['budget_from']} 才開始算（{a['period_label']} 只管得到 "
                   f"{int(a['coverage'] * 100)}%），之前的我只記帳，沒算你頭上。")
    for L in a["lenses"]:
        mark = "←這個最緊，聽它的" if L["name"] == a["binding"] else ""
        out.append(f"{L['name']}：${L['value']:,.0f}　{L['why']} {mark}".rstrip())
    if a["gentle_floor_applied"]:
        out.append(f"不過我沒有壓到那麼低——再省也要能過日子，拉回計畫的一半 ${a['allowance']:,.0f}。")
    if a.get("savings_skipped"):
        out.append(f"這期錢不夠，我先不扣存錢的 ${a['savings_skipped']:,.0f}——記成存錢欠帳，"
                   "之後有多的先補回去。先砍存錢，不是先砍你吃飯。")
    if a["shock_load"]["per_period"] > 0:
        out.append(f"另外扣掉這期要還自己的 ${a['shock_load']['per_period']:,.0f}"
                   f"（之前自己造成的支出，分期還）。")
        if a["shock_load"]["over_cap"]:
            out.append("老實說這些「分期」已經超過收入的 15%，不是在攤平、是在拖，該面對了。")
    if a["deficit"]:
        if a["deficit_kind"] == "timing":
            out.append("這期本來就不夠，但錢在路上——缺口先從水位墊，等款進來補回去。")
        else:
            out.append("這期不夠，而且沒有款要進來。這不是省一點能解決的，是收入的問題。")
    if a["savings_debt"] > 0:
        out.append(f"存錢欠帳目前 ${a['savings_debt']:,.0f}，有多的先補這裡。")
    note = TAX.deadline_note(a["tax"])
    if note:
        out.append(note)
    return out

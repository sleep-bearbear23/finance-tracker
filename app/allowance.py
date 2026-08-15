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
from . import stability as STAB
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


def observed(key: str, begin: date | None) -> float:
    """How much of a period we have *data* for — which is not the same as how much of it
    the budget *governs*.

    A period that ended before 起算日 was fully observed: the records are complete, the
    budget just wasn't in force yet. Treating those as 0%-covered made the trajectory lens
    skip every historical period and report a median discretionary spend of $0 — against
    $4,500 of real spending since May. Only the period 起算日 lands inside is partial."""
    lo, hi = P.key_bounds(key)
    if not begin or begin <= lo or begin > hi:
        return 1.0
    return ((hi - begin).days + 1) / P.days_in(key)


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
async def emergency_target(session, survival_monthly: float | None = None) -> dict:
    """How big the emergency fund should be, recomputed from Momo's own instability.

    A hand-typed target ($10,000? $20,000?) can't answer "am I safe" because safety
    depends on how erratic the work is. If she has explicitly pinned a number we honour
    it; otherwise :mod:`app.stability` measures it and shows its working."""
    pinned = await get_kv(session, EMERG_KEY + "_pinned")
    if pinned:
        try:
            return {"target": float(pinned), "months": None, "pinned": True,
                    "why": ["這是你自己指定的數字，我就照這個算。"], "components": {}}
        except (TypeError, ValueError):
            pass
    if survival_monthly:
        out = await STAB.emergency_target(session, survival_monthly)
        out["pinned"] = False
        return out
    try:
        legacy = float(await get_kv(session, EMERG_KEY) or EMERGENCY_TARGET_DEFAULT)
    except (TypeError, ValueError):
        legacy = EMERGENCY_TARGET_DEFAULT
    return {"target": legacy, "months": None, "pinned": True, "why": [], "components": {}}


def ladder(survival_monthly: float, target: float) -> list[dict]:
    out = [{"name": n, "months": m, "amount": round(survival_monthly * m, 2)}
           for n, m in RUNGS]
    out.append({"name": "目標", "months": round(target / survival_monthly, 1)
                if survival_monthly else None, "amount": round(target, 2)})
    return out


def rung_below(rungs: list[dict], amount: float) -> dict | None:
    """The highest rung the pile currently clears. This is where Momo is STANDING — a
    scoreboard, not an instruction. It used to also be the floor the cushion lens
    defended, which is the bug :func:`defended_floor` exists to undo."""
    cleared = [r for r in rungs if amount >= r["amount"]]
    return cleared[-1] if cleared else None


#: How many months of survival the cushion lens defends by default. One — a real bottom,
#: reachable, and the rest of the pile is hers to spread across the periods until money
#: lands. Climbing to two is a decision she makes, not a thing a deposit does to her.
DEFEND_KEY = "cfg_defend_months"
DEFEND_DEFAULT = 1.0


async def defend_months(session) -> float:
    raw = await get_kv(session, DEFEND_KEY)
    try:
        return max(0.0, float(raw)) if raw else DEFEND_DEFAULT
    except (TypeError, ValueError):
        return DEFEND_DEFAULT


async def set_defend_months(session, months: float) -> float:
    months = max(0.0, min(12.0, float(months)))
    await set_kv(session, DEFEND_KEY, str(months))
    return months


def defended_floor(survival_monthly: float, months: float, cash: float) -> float:
    """The water level the cushion lens holds back — chosen, not tripped over.

    The old rule defended whichever rung the pile happened to clear, and that made the
    allowance a sawtooth. Every crossing reclassified everything above the previous rung
    as untouchable in one step, so getting richer cratered the line: at $4,000 Momo could
    spend $147 a period and at $4,500 she could spend $3. Three cliffs across the range
    she actually lives in, plus an inversion at the bottom where having less than one
    month of survival defended nothing at all and therefore read as the most comfortable
    state of all.

    It surfaced in the worst possible way. Netting her mother's paybacks and dropping
    reimbursable work costs out of the survival floor made the model strictly more
    accurate — and cut her allowance 62%, from $221 to $85, because the corrected floor
    slid her across the second rung. Nothing about her life changed. The rung moved.

    So the floor is a standing decision now, defaulting to one month. Above it the pile is
    hers to spread. Below it there is nothing spare and the answer is zero — which is not
    an instruction to starve but a handoff: :func:`analytics.dip_view` takes over and says
    what surviving the rest of the period costs and which layer it comes out of.
    """
    return min(max(0.0, survival_monthly * months), max(0.0, cash))


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


def _scale(lens: dict, frac: float) -> dict:
    """Give back only the share of a full period's number that the budget governs."""
    if frac >= 1.0 or lens.get("value") is None:
        return lens                          # an abstaining lens stays abstained
    out = {**lens, "value": round(lens["value"] * frac, 2), "full_value": lens["value"]}
    out["why"] = f"{lens['why']}，再乘上這期實際管到的 {int(frac * 100)}%"
    return out


def _plan(income_after_tax: float, fixed_p: float, savings_p: float) -> dict:
    val = income_after_tax - fixed_p - savings_p
    return {"name": "計畫", "value": round(val, 2),
            "why": (f"扣完稅的收入 ${income_after_tax:,.0f} − 固定 ${fixed_p:,.0f} "
                    f"− 存錢 ${savings_p:,.0f}")}


def _cushion(free: float, floor: float, periods_to_money: int,
             rung: str | None = None) -> dict:
    """Expected income enters here and ONLY here — as `periods_to_money`, the number of
    half-months the pile has to stretch. A bigger booked gig makes the wait shorter; it
    never makes the pile bigger."""
    n = max(1, periods_to_money)
    val = (free - floor) / n
    held = f"守住的水位 ${floor:,.0f}" + (f"（{rung}）" if rung else "")
    return {"name": "水位", "value": round(val, 2),
            "why": f"（可動用 ${free:,.0f} − {held}）÷ 撐 {n} 期"}


def _trajectory(recent_median: float, drift_per_period: float,
                observations: int = 1) -> dict:
    """What she's really been spending, pulled down when spendable cash is sliding.

    With zero observations it ABSTAINS instead of voting. It used to vote −$109 from
    drift alone on an empty window — a lens that has seen nothing has no opinion about
    her habits, and Momo's rule for missing data is 「say she doesn't know」, not
    "guess in whichever direction the slope points"."""
    if observations <= 0:
        return {"name": "軌跡", "value": None, "abstain": True,
                "why": "這段時間沒有可看的支出紀錄——沒資料就不投票"}
    val = recent_median + min(0.0, drift_per_period)
    why = f"最近半個月中位數 ${recent_median:,.0f}"
    if drift_per_period < 0:
        why += f"，但可動用的錢每期掉 ${abs(drift_per_period):,.0f}，往下修"
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
    spread = 0.0
    for t in rows:
        if not budget.is_discretionary(t):
            continue
        d = budget.eff_date(t)
        if not d or d < lo or d > hi:
            continue
        k = P.key_for(d)
        if k not in per:
            continue
        # 媽媽的回款 belong to the whole window, not the fortnight the transfer cleared in
        if budget.spreads_over_window(t):
            spread += budget.spend_amount(t)
        else:
            per[k] += budget.spend_amount(t)
    if keys:
        share = spread / len(keys)
        for k in keys:
            per[k] += share
    begin = await start_date(session)
    vals = []
    for k in keys:
        frac = observed(k, begin)      # data coverage, NOT budget governance
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


async def _net_drift(session, window_days: int = 120) -> float:
    """Net-worth change per half-month over RECENT snapshots. Negative = draining.

    Windowed on purpose: the snapshot table reaches back to 2025, and a nine-month slope
    says nothing useful about how this fortnight is going."""
    from sqlalchemy import select

    from .models import Snapshot
    snaps = (await session.execute(select(Snapshot).order_by(Snapshot.day))).scalars().all()
    if len(snaps) < 2:
        return 0.0
    cutoff = (now().date() - timedelta(days=window_days)).isoformat()
    recent = [s for s in snaps if s.day >= cutoff]
    if len(recent) >= 2:
        snaps = recent
    first, last = snaps[0], snaps[-1]
    try:
        d0, d1 = date.fromisoformat(first.day), date.fromisoformat(last.day)
    except ValueError:
        return 0.0
    days = (d1 - d0).days
    if days < 7:
        return 0.0
    # Spendable cash, not net worth: a brokerage dip is not a reason to shrink the
    # grocery line. Falls back to net worth only for old snapshots that never stored cash.
    v0 = first.cash if first.cash is not None else first.net_worth
    v1 = last.cash if last.cash is not None else last.net_worth
    per_day = (v1 - v0) / days
    return round(per_day * 15.2, 2)   # one half-month


async def _periods_to_money(session, today: date) -> tuple[int, dict | None]:
    """How many half-months until the next payment is expected to land.

    Padded by 14 days because productions are late — Momo asked for that explicitly, and
    the padding only ever makes the cushion lens tighter."""
    pend = await prefs.pending_invoices(session)
    best, item = None, None
    for p in pend:
        # one landing model for the whole app now — see prefs.landing()
        land = prefs.landing(p)
        if land is None or land < today:
            continue
        if best is None or land < best:
            best, item = land, p
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
    # Costs stay at FULL period size here. A partial period is handled by scaling each
    # lens's *result* (below), not its inputs — pro-rating the costs but not the income
    # made a part-governed period look richer than a whole one, which is backwards.
    fixed_p = P.split_monthly(fixed_monthly, key)
    savings_p = budget.savings_for(prefs_["savings_amount"], prefs_["savings_cadence"],
                                   key, income_p)

    # the pile
    nw = await networth.compute(session)
    # The lean floor used to be the constant above. Momo asked for these numbers to be
    # computed rather than typed, and this one feeds the emergency target — a floor set too
    # low quietly shrinks the fund she is supposed to be building toward.
    try:
        _flex = await FX.observed_flex(session)
        lean_monthly = _flex["lean"] if _flex.get("enough") and _flex["lean"] > 0 \
            else LEAN_FLEX_MONTHLY
    except Exception:
        lean_monthly = LEAN_FLEX_MONTHLY
    survival_monthly = fixed_monthly + lean_monthly
    emerg = await emergency_target(session, survival_monthly)
    emerg_target = emerg["target"]
    rungs = ladder(survival_monthly, emerg_target)

    # Cash Momo could actually deploy: liquid, minus the card she owes, minus tax that
    # isn't hers. The emergency fund stays IN — the ladder is what protects it.
    reserve_total = round(nw["spendable"] - nw["debts"] - tax_st["outstanding"], 2)
    # WHERE SHE IS versus WHAT SHE DEFENDS. These used to be the same value, which is what
    # made the allowance a sawtooth — see defended_floor(). Standing is the scoreboard now
    # and has no effect on the number.
    standing = rung_below(rungs, reserve_total)
    dm = await defend_months(session)
    floor = defended_floor(survival_monthly, dm, reserve_total)
    defended = next((r for r in rungs if abs(r["months"] - dm) < 0.05), None)

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

    # Each lens answers "what does a whole period support?", then we hand Momo only the
    # share of it this budget actually governs. On a first day of 8/11 that's 5 days of a
    # 15-day period, so she gets a third of the period's number — not all of it.
    lenses = [_scale(L, frac) for L in (
        _plan(income_after_tax, fixed_p, savings_p),
        _cushion(reserve_total, floor, periods_out, defended["name"] if defended else None),
        _trajectory(recent_med, drift, observations=len(recent_rows)),
    )]
    plan_l, cush_l, traj_l = lenses
    plan_val, cush_val, traj_val = (L["value"] for L in lenses)

    # 可以花 comes from what the CASH supports. 計畫 and 軌跡 can only ever tighten it.
    #
    # Taking "smallest wins" literally was wrong when a lens goes negative. A negative
    # 計畫 doesn't mean "eat nothing this fortnight" — it means income doesn't cover fixed
    # costs, which is a fact about the month and says nothing about groceries. Rendering
    # that diagnosis inside the box labelled 可以花 read as "starve", which is not what
    # any of this is for. Negative lenses now report a shortfall instead of setting it.
    capacity = max(0.0, cush_val or 0.0)
    binding = cush_l
    for L in (plan_l, traj_l):
        if L["value"] is not None and L["value"] > 0 and L["value"] < capacity:
            capacity, binding = L["value"], L

    # a lean period should still be livable — only meaningful when the plan is positive
    gentle = plan_val * GENTLE_FLOOR if plan_val > 0 else 0.0
    if 0 < gentle and capacity < gentle:
        capacity = min(gentle, max(0.0, cush_val))

    raw = capacity
    recommended = capacity

    # shocks Momo owes herself
    load = await shock_load(session, income_p)
    recommended = max(0.0, recommended - load["per_period"])

    # what this period costs regardless of any spending decision
    shortfall = round(min(0.0, plan_val), 2)
    trend_warning = traj_val < 0 or traj_val < capacity

    spent = await _spent(session, key, from_day, hi)
    remaining = round(recommended - spent, 2)
    days_left = max(0, (hi - today).days + 1) if lo <= today <= hi else 0
    dv = daily_view(recommended, from_day, hi, today,
                    await _spent_by_day(session, from_day, hi),
                    await grants(session, key))

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
        "fixed_monthly": fixed_monthly,
        "fixed_period": round(fixed_p * frac, 2), "fixed_period_full": round(fixed_p, 2),
        "savings_period": round(savings_p * frac, 2),
        "savings_skipped": savings_skipped,
        "savings_debt": await savings_debt(session),

        "lenses": lenses,
        "binding": binding["name"],
        "binding_why": binding["why"],
        "shortfall": shortfall,          # 這期的缺口 — happens whatever Momo does
        "trend_warning": trend_warning,  # 趨勢 — recent burn vs where net worth is going
        "emergency": emerg,
        "raw_allowance": round(raw, 2),
        "allowance": round(recommended, 2),
        "gentle_floor_applied": bool(raw < gentle and plan_val > 0),

        "spent": spent,
        "remaining": remaining,
        "per_day_left": round(max(0.0, remaining) / days_left, 2) if days_left else None,
        "daily": dv,
        "pct_used": round(100 * spent / recommended, 1) if recommended > 0 else None,

        "reserve_total": reserve_total,
        "standing_rung": standing,
        "defend_months": dm,
        "defended_rung": defended,
        "defended_floor": round(floor, 2),
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


async def _spent_by_day(session, from_day: date, hi: date) -> dict[date, float]:
    """The same spend, split by day — what the daily budget and the pool are built on."""
    from sqlalchemy import select

    from .models import Transaction
    rows = (await session.execute(select(Transaction))).scalars().all()
    out: dict[date, float] = {}
    for t in rows:
        if not budget.is_discretionary(t):
            continue
        d = budget.eff_date(t)
        if d and from_day <= d <= hi:
            out[d] = round(out.get(d, 0.0) + budget.spend_amount(t), 2)
    return out


# ── 每天一條線，省下來的進口袋 ────────────────────────────────────────
#: Grants Momo has asked for: a raise to the daily figure, funded only out of the pool.
GRANTS_KEY = "cfg_daily_grants"


async def grants(session, key: str | None = None) -> list[dict]:
    raw = await get_kv(session, GRANTS_KEY)
    try:
        rows = json.loads(raw) if raw else []
    except (TypeError, ValueError):
        rows = []
    rows = [r for r in rows if isinstance(r, dict)]
    return [r for r in rows if key is None or r.get("period") == key]


async def add_grant(session, key: str, amount: float, start: date, until: date) -> dict:
    rows = await grants(session)
    row = {"period": key, "amount": round(float(amount), 2),
           "from": start.isoformat(), "until": until.isoformat()}
    rows.append(row)
    await set_kv(session, GRANTS_KEY, json.dumps(rows, ensure_ascii=False))
    return row


def grant_on(rows: list[dict], day: date) -> float:
    """How much extra Momo granted herself for this particular day."""
    total = 0.0
    for r in rows:
        try:
            lo = date.fromisoformat(r["from"])
            hi = date.fromisoformat(r["until"])
        except (KeyError, TypeError, ValueError):
            continue
        if lo <= day <= hi:
            total += float(r.get("amount") or 0)
    return round(total, 2)


def daily_view(line: float, from_day: date, hi: date, today: date,
               by_day: dict[date, float], grant_rows: list[dict]) -> dict:
    """One line per day, and what you didn't spend yesterday kept for later.

    Momo: "if I spend nothing for the first 14 days, I have the whole budget to spend for
    the last day." A period-wide number is a slow-motion trap — it reads as permission all
    month and then as a cliff, and it gives her nothing to decide against on a Tuesday.

    So the line is divided by the days it covers and that figure holds steady. What a day
    does not use is not lost and is not silently available either: it lands in 本期口袋,
    which is a real balance she can see and spend on purpose rather than by drift.

    The pool is the running sum of (BASE − spent) over closed days — deliberately not
    (allowed − spent). A grant lifts the ceiling for a day; it is not a deposit. Counting
    it as one meant a raise Momo asked for and then did not use would ADD to the pocket,
    which would let her mint money by requesting raises she never spent. Spending above
    base draws the pocket down by itself, so the grant needs no second ledger — it only
    has to be affordable at the moment she asks, which is what the tool checks.
    """
    days_in = (hi - from_day).days + 1
    base = round(line / days_in, 2) if days_in > 0 else 0.0

    pool = 0.0
    d = from_day
    while d < today and d <= hi:
        pool += base - by_day.get(d, 0.0)
        d += timedelta(days=1)
    pool = round(pool, 2)

    in_span = from_day <= today <= hi
    bump = grant_on(grant_rows, today) if in_span else 0.0
    today_allowed = round(base + bump, 2) if in_span else 0.0
    today_spent = round(by_day.get(today, 0.0), 2) if in_span else 0.0
    days_left = (hi - today).days + 1 if in_span else 0

    return {
        "daily_base": base,
        "daily_today": today_allowed,
        "daily_bump": round(bump, 2),
        "today_spent": today_spent,
        "today_left": round(today_allowed - today_spent, 2),
        "pool": pool,
        "days_closed": max(0, (min(today, hi + timedelta(days=1)) - from_day).days),
        "days_left": days_left,
        # what a raise of $1/day for the rest of the period would cost the pool
        "raise_unit_cost": days_left,
        "note": ("每天一條線，沒花完的留在口袋。想要哪天多花一點，就從口袋拿——"
                 "口袋是空的就不行，那不是小氣，是那筆錢還沒存出來。"),
    }


async def closure(session, key: str | None = None) -> dict:
    """The end-of-period review: how the fortnight actually went, day by day.

    Momo asked for a closing meeting rather than a silent rollover — "give me summary of my
    performance… we will also decide what to do with the remaining money in the pool, which
    most likely goes to the quarters' goal."

    Graded against the daily line and nothing else, same as the fortnight itself. Her Law
    still holds here: a period where no money arrived is not a period she failed."""
    a = await compute(session, key)
    lo, hi = P.key_bounds(a["period_key"])
    from_day = date.fromisoformat(a["budget_from"])
    by_day = await _spent_by_day(session, from_day, hi)
    gr = await grants(session, a["period_key"])
    base = a["daily"]["daily_base"]
    today = now().date()

    # Same closed-days rule the pool uses, or the two disagree: run mid-period, today is
    # still open and its unspent balance is not in the pocket yet.
    last = hi if today > hi else today - timedelta(days=1)
    days, under, over = [], 0, 0
    for i in range((hi - from_day).days + 1):
        d = from_day + timedelta(days=i)
        if d > last:
            break
        allowed = round(base + grant_on(gr, d), 2)
        got = round(by_day.get(d, 0.0), 2)
        days.append({"date": d.isoformat(), "allowed": allowed, "spent": got,
                     "left": round(allowed - got, 2), "pool_delta": round(base - got, 2)})
        if got <= allowed:
            under += 1
        else:
            over += 1

    # against BASE, not against the raised ceiling — see daily_view
    pool = round(sum(x["pool_delta"] for x in days), 2)
    return {
        "period": a["period_key"], "label": a["period_label"],
        "start": a["budget_from"], "end": hi.isoformat(),
        "closed": today > hi,
        "daily_base": base, "days": days,
        "days_under": under, "days_over": over,
        "spent": a["spent"], "line": a["allowance"],
        "pool": pool,
        "grants": gr,
        "verdict": "under" if a["spent"] <= a["allowance"] else "over",
        "binding": a.get("binding"),
        "ask": ("口袋裡還有 ${:,.0f}。要放到這一季的目標裡，還是留著下一期用？"
                .format(pool) if pool > 0 else
                "這一期口袋是空的，沒有東西要分配。"),
    }


def explain(a: dict) -> list[str]:
    """Her actual reasoning, in order, in words. If she can't say it she shouldn't use it."""
    out = []
    if a["partial"]:
        out.append(f"這期我從 {a['budget_from']} 才開始算（{a['period_label']} 只管得到 "
                   f"{int(a['coverage'] * 100)}%），之前的我只記帳，沒算你頭上。")
    out.append(f"可以花 ${a['allowance']:,.0f}"
               + (f"（剩 {a['days_left']} 天，一天大概 ${a['per_day_left']:,.0f}）"
                  if a.get("per_day_left") else "")
               + f"　這是照「{a['binding']}」算的：{a['binding_why']}")
    if a.get("shortfall"):
        out.append(f"這期的缺口 ${abs(a['shortfall']):,.0f}：進來的錢本來就不夠付固定開銷，"
                   "不管你吃不吃飯都會少這麼多，會從水位補。這不是叫你別花，是讓你知道這期在扣老本。")
    for L in a["lenses"]:
        mark = "←可以花是照這個算的" if L["name"] == a["binding"] else ""
        out.append(f"　{L['name']}：${L['value']:,.0f}　{L['why']} {mark}".rstrip())
    if a.get("trend_warning") and a["lenses"][2]["value"] < 0:
        out.append(f"趨勢上要提醒你：最近每期花 ${a['recent_median']:,.0f}，"
                   f"但淨值每期掉 ${abs(a['net_drift_per_period']):,.0f}，這個速度撐不久。")
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
    em = a.get("emergency") or {}
    if em.get("why") and not em.get("pinned"):
        out.append(f"緊急預備金目標 ${em['target']:,.0f}"
                   + (f"（約 {em['months']} 個月的最低開銷）" if em.get("months") else "")
                   + "：" + " ".join(em["why"]))
    note = TAX.deadline_note(a["tax"])
    if note:
        out.append(note)
    return out

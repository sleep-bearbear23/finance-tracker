"""Momo's fixed costs, as rows instead of one remembered number.

The budget used to read a single ``fixed_monthly`` figure Momo had typed in months
earlier — $2,400, which turned out to be a third too high once we counted it off real
statements. A wrong figure here silently distorts two of the three lenses, so it lives
as data now: each line has an amount, a real cadence, and a due date. The same rows
drive the renewal reminders, because "GEICO is $147/month" and "GEICO takes $884 out of
your account in November" are both true and only one of them empties the account.

Sinking funds are deliberately a *separate* list. DMV registration, car repairs and the
Taiwan trip are real and irregular, but folding them in would quietly move the number
Momo agreed to. She can turn them on; they don't turn themselves on.
"""
from __future__ import annotations

import json
from datetime import date

from . import period as P
from .config import now
from .db import get_kv, set_kv

KEY = "cfg_fixed_costs"
KEY_SINKING = "cfg_sinking"
KEY_SINKING_ON = "cfg_sinking_on"

#: how many months one payment covers
CADENCE_MONTHS = {"monthly": 1, "quarterly": 3, "semiannual": 6, "annual": 12}

# Worked out with Momo on 2026-08-11 against 19 months of Apple Card statements plus
# what only she could tell me (rent, and which subscriptions are still alive).
DEFAULTS: list[dict] = [
    # manual: Momo has to actually send this one every month. It is the reason the
    # calendar now carries monthly lines she initiates herself — she asked for the $1,000
    # to her mother to be on there, and it was already a fixed cost, just invisible.
    # since: rent began 2026-07. Without it the splitter amortised $1,000/month back
    # through June and the season opened with ~$400 of phantom rent she never paid.
    {"name": "房租（Zelle 給媽媽）", "amount": 1000.0, "cadence": "monthly",
     "cat": "rent", "where": "Chase", "manual": True, "since": "2026-07-01"},
    {"name": "加油", "amount": 150.0, "cadence": "monthly", "cat": "gas",
     "where": "Apple Card", "note": "一個月 3 次上下，工作要開車"},
    {"name": "Claude 訂閱（含加值）", "amount": 110.0, "cadence": "monthly",
     "cat": "subs", "where": "Apple Card"},
    {"name": "GEICO 車險", "amount": 884.28, "cadence": "semiannual", "cat": "insurance",
     "where": "Apple Card", "next_due": "2026-11-12"},
    {"name": "Railway + API（機器人）", "amount": 40.0, "cadence": "monthly",
     "cat": "subs", "where": "Apple Card"},
    {"name": "Ultra Mobile 電話", "amount": 186.0, "cadence": "semiannual",
     "cat": "phone", "where": "Apple Card", "next_due": "2026-12-28"},
    {"name": "Adobe", "amount": 19.99, "cadence": "monthly", "cat": "subs",
     "where": "Apple Card"},
    {"name": "YouTube（兩個）", "amount": 16.98, "cadence": "monthly", "cat": "subs",
     "where": "Apple Card"},
    {"name": "iCloud", "amount": 5.99, "cadence": "monthly", "cat": "subs",
     "where": "Apple Card"},
]

# Real, irregular, and currently absorbed as shocks. Off by default — see module docstring.
SINKING_DEFAULTS: list[dict] = [
    {"name": "DMV 牌照更新", "amount": 371.0, "cadence": "annual",
     "cat": "fees", "next_due": "2027-08-01", "note": "2026-08-11 剛換過"},
    {"name": "修車（保養約到 2028，只剩維修）", "amount": 1200.0, "cadence": "annual",
     "cat": "car", "note": "抓一年一次；爸媽有時會出"},
]


def _monthly(row: dict) -> float:
    """One line's cost expressed per month, whatever its real billing cycle."""
    n = CADENCE_MONTHS.get(row.get("cadence") or "monthly", 1)
    try:
        return float(row.get("amount") or 0) / n
    except (TypeError, ValueError):
        return 0.0


def _load(raw: str | None, fallback: list[dict]) -> list[dict]:
    try:
        got = json.loads(raw) if raw else None
    except (TypeError, ValueError):
        got = None
    # None = never saved → the defaults. An EMPTY list is a decision — deleting your
    # last fixed cost used to resurrect all nine defaults, because [] is falsy.
    return got if isinstance(got, list) else [dict(r) for r in fallback]


async def rows(session, include_sinking: bool | None = None) -> list[dict]:
    """The live list, each row annotated with its per-month cost."""
    out = _load(await get_kv(session, KEY), DEFAULTS)
    for r in out:
        r["monthly"] = round(_monthly(r), 2)
        r["sinking"] = False
    if include_sinking is None:
        include_sinking = (await get_kv(session, KEY_SINKING_ON)) == "1"
    if include_sinking:
        for r in _load(await get_kv(session, KEY_SINKING), SINKING_DEFAULTS):
            r["monthly"] = round(_monthly(r), 2)
            r["sinking"] = True
            out.append(r)
    return out


async def sinking_rows(session) -> list[dict]:
    out = _load(await get_kv(session, KEY_SINKING), SINKING_DEFAULTS)
    for r in out:
        r["monthly"] = round(_monthly(r), 2)
        r["sinking"] = True
    return out


async def save(session, new_rows: list[dict]) -> None:
    clean = []
    for r in new_rows or []:
        try:
            amt = float(r.get("amount"))
        except (TypeError, ValueError, AttributeError):
            continue
        clean.append({k: r.get(k) for k in
                      ("name", "amount", "cadence", "cat", "where", "next_due", "note",
                       "manual", "since")
                      if r.get(k) is not None} | {"amount": amt})
    await set_kv(session, KEY, json.dumps(clean, ensure_ascii=False))


def active(r: dict, on) -> bool:
    """Is this cost alive on a given date? A row with no ``since`` always was.

    ``since`` exists because a fixed cost has a birthday: Momo's rent started 2026-07,
    and a splitter that amortises $1,000/month across every period it can see invented
    ~$400 of June rent. A date the cost didn't exist on is not a cheaper version of the
    cost — it's no cost at all."""
    s = r.get("since")
    if not s or on is None:
        return True
    try:
        return date.fromisoformat(str(s)[:10]) <= on
    except ValueError:
        return True


async def monthly_total(session, include_sinking: bool | None = None, on=None) -> float:
    return round(sum(r["monthly"] for r in await rows(session, include_sinking)
                     if active(r, on)), 2)


async def per_period(session, key: str, include_sinking: bool | None = None) -> float:
    """Charged to one half-month, weighted by real day count (15/31 vs 16/31).

    Rows are filtered by whether they were alive at the period's start, so a cost that
    began in July charges nothing to June."""
    lo, _ = P.key_bounds(key)
    return round(P.split_monthly(
        await monthly_total(session, include_sinking, on=lo), key), 2)


async def by_treatment(session) -> dict[str, float]:
    """Monthly cost grouped by category id — used to reconcile the stated figure against
    what the ledger actually shows."""
    agg: dict[str, float] = {}
    for r in await rows(session):
        agg[r.get("cat") or "other"] = round(agg.get(r.get("cat") or "other", 0.0)
                                             + r["monthly"], 2)
    return agg


# ── the renewal calendar ─────────────────────────────────────────────
def _advance(due: date, cadence: str, today: date) -> date:
    """Roll a stale due date forward until it's in the future."""
    n = CADENCE_MONTHS.get(cadence or "monthly", 1)
    y, m = due.year, due.month
    d = due
    guard = 0
    while d < today and guard < 60:
        m += n
        y, m = y + (m - 1) // 12, (m - 1) % 12 + 1
        day = min(due.day, [31, 29 if y % 4 == 0 and (y % 100 or y % 400 == 0) else 28,
                            31, 30, 31, 30, 31, 31, 30, 31, 30, 31][m - 1])
        d = date(y, m, day)
        guard += 1
    return d


async def renewals(session, within_days: int = 45, include_sinking: bool = True) -> list[dict]:
    """Non-monthly lines coming due soon — the ones that empty an account in one go.

    Monthly lines are excluded on purpose: Momo doesn't need a reminder that Adobe
    charges again, she needs one that GEICO wants $884 in November."""
    today = now().date()
    out = []
    for r in await rows(session, include_sinking=include_sinking):
        if (r.get("cadence") or "monthly") == "monthly":
            continue
        raw = r.get("next_due")
        if not raw:
            continue
        try:
            due = _advance(date.fromisoformat(str(raw)[:10]), r["cadence"], today)
        except ValueError:
            continue
        days = (due - today).days
        if days <= within_days:
            out.append({"name": r["name"], "amount": float(r.get("amount") or 0),
                        "due": due.isoformat(), "days": days, "cat": r.get("cat"),
                        "where": r.get("where"), "sinking": r.get("sinking", False)})
    return sorted(out, key=lambda x: x["days"])


async def calendar(session, months: int = 14) -> list[dict]:
    """Every dated hit in the next N months, so a dry spell can be seen coming.

    Monthly lines are still skipped — Momo does not need twelve reminders a year that Adobe
    charges again — with one exception: a line she has to go and DO. The $1,000 Zelle to her
    mother is not an auto-debit; if it is not on the calendar it is a thing she can forget,
    which is the whole reason she asked for it there."""
    today = now().date()
    horizon = date(today.year + (today.month + months - 1) // 12,
                   (today.month + months - 1) % 12 + 1, 1)
    out = []
    for r in await rows(session, include_sinking=True):
        cad = r.get("cadence") or "monthly"
        if cad == "monthly" and r.get("manual"):
            # no stated day: assume the 1st, which is when a monthly transfer usually goes
            try:
                day = date.fromisoformat(str(r["next_due"])[:10]).day if r.get("next_due") else 1
            except ValueError:
                day = 1
            y, m = today.year, today.month
            for _ in range(months):
                d = date(y, m, min(day, 28))
                if d >= today and d < horizon:
                    out.append({"name": r["name"], "amount": float(r.get("amount") or 0),
                                "due": d.isoformat(), "cat": r.get("cat"),
                                "manual": True, "sinking": False})
                y, m = (y + 1, 1) if m == 12 else (y, m + 1)
            continue
        if cad == "monthly" or not r.get("next_due"):
            continue
        try:
            due = _advance(date.fromisoformat(str(r["next_due"])[:10]), cad, today)
        except ValueError:
            continue
        step = CADENCE_MONTHS[cad]
        guard = 0
        while due < horizon and guard < 40:
            out.append({"name": r["name"], "amount": float(r.get("amount") or 0),
                        "due": due.isoformat(), "cat": r.get("cat"),
                        "sinking": r.get("sinking", False)})
            y, m = due.year, due.month + step
            y, m = y + (m - 1) // 12, (m - 1) % 12 + 1
            due = date(y, m, min(due.day, 28))
            guard += 1
    return sorted(out, key=lambda x: x["due"])


# ── what her costs ACTUALLY are, as opposed to what the rows say ─────
#: How many half-months of history to read the observed figures from. Six is a quarter —
#: long enough to smooth a heavy fortnight, short enough that last winter is not voting.
OBSERVED_PERIODS = 6

#: The stated rows and the ledger will never agree exactly. Past this, something is wrong:
#: a row is stale, a bill is missing, or a subscription quietly went up.
DIVERGENCE_FLAG = 0.10


async def _by_period(session, treatments: set[str], periods: int,
                     require_bank: bool = False) -> list[float]:
    """Spend per half-month in the given treatment groups, oldest first.

    ``require_bank`` drops periods the bank feed does not reach. It matters more than it
    sounds: her rent is a Zelle out of Chase, and Chase only backfills 45 days, so a
    six-period window mostly predates the data. Averaging over months where the biggest
    fixed cost is simply absent produces a confident, tidy, wrong number — the first run
    of this reported her fixed life at $702/month against a real $1,521."""
    from . import budget
    from . import facts as F
    from . import taxonomy as T
    f = await F.build(session)
    keys = P.last_n(budget.current_key(), periods + 1)[:-1]   # drop the half we are in
    if require_bank:
        bank = [budget.eff_date(t) for t in f.txns if t.source == "simplefin"]
        bank = [d for d in bank if d]
        if not bank:
            return []
        first = P.key_for(min(bank))
        keys = [k for k in keys if k > first]      # strictly after the first partial period
    per = dict.fromkeys(keys, 0.0)
    spread = 0.0
    for t in f.txns:
        if not budget.is_spend(t):
            continue
        d = budget.eff_date(t)
        if not d:
            continue
        k = P.key_for(d)
        if k not in per:
            continue
        if (T.treatment(t.category) or "") not in treatments:
            continue
        if budget.spreads_over_window(t):
            spread += budget.spend_amount(t)      # negative; see budget.spreads_over_window
        else:
            per[k] += budget.spend_amount(t)
    share = spread / len(keys) if keys else 0.0
    return [round(per[k] + share, 2) for k in keys]


async def observed_fixed_monthly(session, periods: int = OBSERVED_PERIODS) -> dict:
    """What actually left her accounts on 固定-treatment categories, per month.

    Momo asked for the fixed number to be computed rather than typed. It cannot simply
    REPLACE the rows, and the reason is worth keeping: GEICO takes $884 in November and
    Ultra $186 in December, so a trailing three-month window contains neither, and an
    average over it would quietly tell her the fixed life is $147/month cheaper than it
    is. The rows are the only thing that can amortise a lump. So the rows stay the plan,
    and this is the reconciliation — it says when the plan has gone stale."""
    from . import taxonomy as T
    vals = await _by_period(session, {T.FIXED}, periods, require_bank=True)
    if len(vals) < 2:
        return {"monthly": 0.0, "periods": len(vals), "series": vals, "enough": False}
    return {"monthly": round(sum(vals) / len(vals) * 2, 2), "periods": len(vals),
            "series": vals, "enough": True}


async def reconcile(session, periods: int = OBSERVED_PERIODS) -> dict:
    """Plan vs. reality on fixed costs, with a flag when they have drifted apart.

    Compared like for like, which took a correction. Her single biggest fixed cost —
    「ZELLE PAYMENT TO MOM $1,000」 — is booked as a transfer, so it is not spending in the
    ledger and never will be. Measuring the whole stated total against observed card spend
    reported her fixed life at $543/month against a real $1,521 and raised a permanent
    alarm about a $978 hole that does not exist. Only the rows that actually flow through
    as spending are in the comparison; the ones she sends by hand are listed apart."""
    rows_all = await rows(session, include_sinking=False)
    by_hand = [r for r in rows_all if r.get("manual")]
    stated_all = round(sum(r["monthly"] for r in rows_all), 2)
    stated = round(sum(r["monthly"] for r in rows_all if not r.get("manual")), 2)
    obs = await observed_fixed_monthly(session, periods)
    gap = round(obs["monthly"] - stated, 2)
    rel = abs(gap) / stated if stated else 0.0
    return {"stated_all": stated_all, "stated": stated,
            "observed": obs["monthly"], "gap": gap,
            "by_hand": [{"name": r["name"], "monthly": r["monthly"],
                         "since": r.get("since")} for r in by_hand],
            "by_hand_monthly": round(sum(r["monthly"] for r in by_hand
                                         if active(r, now().date())), 2),
            "by_hand_rows": [{"monthly": r["monthly"], "since": r.get("since"),
                              "name": r["name"]} for r in by_hand],
            "periods": obs["periods"], "series": obs["series"],
            "enough": obs.get("enough", False),
            "diverged": bool(obs.get("enough")) and rel > DIVERGENCE_FLAG,
            "note": ("這張表是計畫，右邊是最近 "
                     f"{obs['periods']} 個半月真的刷掉的。自己轉帳的（房租那種）銀行記成轉帳、"
                     "不算支出，所以不放進來比；半年一期的 GEICO、電話費也不會出現在最近幾個月。"
                     "差太多才是有一筆沒記到、或哪一筆漲價了。")}


async def observed_flex(session, periods: int = OBSERVED_PERIODS) -> dict:
    """Her flexible spending, per month: the lean end and the middle of her own record.

    LEAN_FLEX_MONTHLY was a constant I typed ($550), with a comment claiming it came from
    her cheapest months. It did not — nothing recomputed it. This does: the 25th percentile
    of her real half-month flexible spend is a floor she has actually lived on, and it
    moves as she does.

    Only 彈性 and 想要. This used to sweep in every treatment except 固定 and 不算支出,
    which meant 「the most frugal Momo has ever lived」 included gaff tape she bought for a
    shoot and got paid back for, and one-off shocks — and shocks are already carried by
    their own amortised load, so they were counted twice. The floor came out about 14% too
    high, and since every ladder rung and the emergency target are multiples of it, so did
    they. Momo, on 工作 and 不規則: "I agree just cuz I think the past month and moving
    forward, these two is probably not gonna have much" — true today, and the structural
    reason holds in a busy season too."""
    from . import taxonomy as T
    groups = {T.FLEX, T.WANT}
    # Clamp at zero. Now that 媽媽的回款 nets off this bucket, a fortnight where the payback
    # lands after the purchases can total negative — and a negative first quartile would
    # drag the survival floor, and therefore the whole ladder, below zero. Spending less
    # than nothing is not a floor anyone can live on.
    vals = sorted(max(0.0, v) for v in await _by_period(session, groups, periods))
    if len(vals) < 3:
        return {"lean": 0.0, "median": 0.0, "periods": len(vals), "enough": False}
    import statistics
    # a real interpolated quartile — index arithmetic on six values just returns the
    # minimum, and one unusually quiet fortnight should not become the definition of lean
    q1 = statistics.quantiles(vals, n=4)[0] if len(vals) >= 4 else min(vals)
    return {"lean": round(q1 * 2, 2), "median": round(statistics.median(vals) * 2, 2),
            "periods": len(vals), "enough": True, "series": vals}

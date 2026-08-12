"""User financial preferences (fixed costs, savings goal, income profile), stored in the KV table."""
from __future__ import annotations

import json
import re
from datetime import date, timedelta

from .db import get_kv, set_kv


def _f(v, default=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9一-鿿]+", "", (name or "").lower())


async def get_prefs(session) -> dict:
    return {
        "fixed_monthly": _f(await get_kv(session, "cfg_fixed_monthly"), 0.0),
        "savings_amount": _f(await get_kv(session, "cfg_savings_amount"), 0.0),
        "savings_cadence": await get_kv(session, "cfg_savings_cadence", "biweekly"),
    }


async def set_prefs(session, values=None, *, fixed_monthly=None, savings_amount=None,
                    savings_cadence=None) -> None:
    """Accepts keywords or a plain dict — passing a dict positionally used to be silently
    written into cfg_fixed_monthly as a stringified dict, which is the kind of quiet
    corruption that shows up three screens later as a wrong allowance."""
    if isinstance(values, dict):
        fixed_monthly = values.get("fixed_monthly", fixed_monthly)
        savings_amount = values.get("savings_amount", savings_amount)
        savings_cadence = values.get("savings_cadence", savings_cadence)
    elif values is not None:
        raise TypeError("set_prefs(values) takes a dict")
    if fixed_monthly is not None:
        await set_kv(session, "cfg_fixed_monthly", str(fixed_monthly))
    if savings_amount is not None:
        await set_kv(session, "cfg_savings_amount", str(savings_amount))
    if savings_cadence is not None:
        await set_kv(session, "cfg_savings_cadence", savings_cadence)


def _load_list(raw):
    try:
        v = json.loads(raw) if raw else []
        return v if isinstance(v, list) else []
    except Exception:
        return []


async def get_income_profile(session) -> dict:
    """The forward-looking income picture Momo set in the starter pack."""
    return {
        "monthly_baseline": _f(await get_kv(session, "cfg_monthly_baseline"), 0.0),
        "upcoming": _load_list(await get_kv(session, "cfg_upcoming")),
        "accounts": _load_list(await get_kv(session, "cfg_accounts")),
        "ytd_income": _f(await get_kv(session, "cfg_ytd_income"), 0.0),
        "cash_on_hand": _f(await get_kv(session, "cfg_cash_on_hand"), 0.0),
        "emergency_target": _f(await get_kv(session, "cfg_emergency_target"), 0.0),
        "total_debt": _f(await get_kv(session, "cfg_total_debt"), 0.0),
        "savings_balance": _f(await get_kv(session, "cfg_savings_balance"), 0.0),
    }


async def set_income_profile(session, data: dict) -> None:
    scalar = {
        "cfg_monthly_baseline": data.get("monthly_baseline"),
        "cfg_ytd_income": data.get("ytd_income"),
        "cfg_cash_on_hand": data.get("cash_on_hand"),
        "cfg_emergency_target": data.get("emergency_target"),
        "cfg_total_debt": data.get("total_debt"),
        "cfg_savings_balance": data.get("savings_balance"),
    }
    for k, v in scalar.items():
        if v is not None:
            await set_kv(session, k, str(v))
    up = data.get("upcoming")
    if up is not None:
        # keep only clean {amount, when, note, status} rows
        clean = []
        for u in (up if isinstance(up, list) else []):
            try:
                amt = float(u.get("amount"))
            except (TypeError, ValueError, AttributeError):
                continue
            if amt > 0:
                # keep every field a booking can carry — stripping to four keys here would
                # silently wipe stage / wrap date / day count on the next profile paste
                row = {"amount": amt, "when": u.get("when"), "note": u.get("note"),
                       "status": u.get("status") or "pending"}
                for k in ("stage", "wrapped_on", "days", "confidence", "expect_on"):
                    if u.get(k) is not None:
                        row[k] = u[k]
                clean.append(row)
        await set_kv(session, "cfg_upcoming", json.dumps(clean))

    accts = data.get("accounts")
    if accts is not None:
        clean = []
        for a in (accts if isinstance(accts, list) else []):
            try:
                amt = float(a.get("amount"))
            except (TypeError, ValueError, AttributeError):
                amt = 0.0
            typ = "credit" if a.get("type") == "credit" else "cash"
            # keep credit cards even at $0 owed (the card exists; balance just moves),
            # and any named cash account with money in it
            if amt > 0 or (typ == "credit" and a.get("name")):
                clean.append({"name": a.get("name"), "type": typ, "amount": max(amt, 0.0)})
        await set_kv(session, "cfg_accounts", json.dumps(clean))
        await _refresh_totals(session, clean)


async def _refresh_totals(session, accts) -> None:
    cash = sum(float(a["amount"]) for a in accts if a.get("type") != "credit")
    debt = sum(float(a["amount"]) for a in accts if a.get("type") == "credit")
    await set_kv(session, "cfg_cash_on_hand", str(cash))
    await set_kv(session, "cfg_total_debt", str(debt))


async def get_income_sources(session) -> list:
    """Normalized name tokens of Momo's known work payers (productions, producers)."""
    return _load_list(await get_kv(session, "cfg_income_sources"))


async def add_income_source(session, name) -> str | None:
    name = (name or "").strip()
    if not name:
        return None
    items = _load_list(await get_kv(session, "cfg_income_sources"))
    key = _norm(name)
    if key and not any(_norm(x) == key for x in items):
        items.append(name)
        await set_kv(session, "cfg_income_sources", json.dumps(items))
    return name


async def is_work_income_source(session, desc: str) -> bool:
    """True if a bank/description looks like it came from a known work payer.
    Uses normalized substring match, so 'ZELLE PAYMENT FROM JUMP DEER MEDIA INC 123'
    still matches the stored 'Jump Deer Media'."""
    d = _norm(desc)
    if not d:
        return False
    for src in await get_income_sources(session):
        s = _norm(src)
        if len(s) >= 4 and s in d:
            return True
    return False


# ── when does booked money actually land, and how much of it should we believe ──
#: Days from wrap to money. Momo's own book is the evidence: Prince in Workboots wrapped in
#: mid-May and was still unpaid in August. Net-45 is not pessimism, it is her median
#: experience, and the padding only ever makes the plan tighter.
PAY_LAG_DAYS = 45

#: Where a job is in its life. Momo specified Booked → Wrapped → Paid at the very start;
#: only Paid was ever built, so a shoot that has not happened yet was counted exactly like
#: one that wrapped in May. These are the risks that are actually different:
#:
#:   booked    the shoot has not happened. Vertical schedules move, get cut, get dropped —
#:             production risk sits on top of collection risk
#:   wrapped   the work exists and cannot be un-done. Only collection is in question
#:   invoiced  the paperwork is in and the clock has formally started
#:
#: Lateness decays these further; the two multiply, because they are separate risks.
#:
#: Momo corrected the levels from her own experience: wrapped work should be trusted
#: "almost 100%", and booked "still on the high end, cuz it's rare that a booked show would
#: end up not happening." The first pass had 70/90, which priced a vertical shoot on her
#: calendar as a coin-flip-and-a-half and made the whole forecast read gloomier than her
#: actual track record.
STAGE_CONFIDENCE = {"booked": 0.85, "wrapped": 0.95, "invoiced": 0.97}
STAGES = tuple(STAGE_CONFIDENCE)
DEFAULT_STAGE = "booked"

#: How much of an invoice survives being late, on top of its stage. Prince in Workboots
#: counted at 100% told the 需要賺 index she could survive three months on $0 of new work.
CONFIDENCE_STEPS = ((0, 1.00), (30, 0.80), (60, 0.60), (90, 0.40), (10**6, 0.25))

#: Nothing is ever worth literally nothing while it is still owed — but it can get close.
CONFIDENCE_FLOOR = 0.10


def stage_of(inv: dict) -> str:
    st = str(inv.get("stage") or "").lower()
    if st in STAGE_CONFIDENCE:
        return st
    # a job with a wrap date on it has, by definition, wrapped
    return "wrapped" if inv.get("wrapped_on") else DEFAULT_STAGE


def landing(inv: dict, lag_days: int = PAY_LAG_DAYS) -> date | None:
    """The day an expected payment should realistically arrive.

    Three sources, in order of authority:

      expect_on    a date a production actually gave her. Beats every model — Momo often
                   knows ("they said end of the month"), and until this existed she had no
                   way to say so and everything was forced through the generic lag.
      wrapped_on   the day the shoot finished, plus the lag. 「9/2 殺青」 and 「9/28 殺青」
                   are not the same money.
      when         the work month, assumed to have finished at month end, plus the lag.

    This one function is the only place that opinion lives. It used to exist three times
    with three different answers, so the calendar said a September job landed on 10/14,
    the income page booked it in September, and the horizon test used 10/1."""
    told = str(inv.get("expect_on") or "")[:10]
    if len(told) == 10:
        try:
            return date.fromisoformat(told)
        except ValueError:
            pass
    w = str(inv.get("wrapped_on") or "")[:10]
    if len(w) == 10:
        try:
            return date.fromisoformat(w) + timedelta(days=lag_days)
        except ValueError:
            pass
    m = str(inv.get("when") or "")[:7]
    try:
        y, mo = int(m[:4]), int(m[5:7])
        month_end = date(y + (mo // 12), (mo % 12) + 1, 1) - timedelta(days=1)
    except (ValueError, IndexError):
        return None
    return month_end + timedelta(days=lag_days)


def confidence(inv: dict, today: date | None = None) -> float:
    """How much of this invoice a plan is allowed to count on.

    Three inputs, in order of authority:

      her own view   an explicit ``confidence`` on the row wins outright. Momo knows which
                     productions pay and which ones need chasing; the model does not.
      stage          booked / wrapped / invoiced — how much of the risk is already retired
      lateness       how far past its expected landing it has drifted

    Stage and lateness multiply. They are independent: a shoot can fall through *and* a
    production can sit on an invoice, and a booked gig that is also two months overdue is
    genuinely worse than either alone.
    """
    own = inv.get("confidence")
    if own is not None:
        try:
            return min(1.0, max(0.0, float(own)))
        except (TypeError, ValueError):
            pass
    base = STAGE_CONFIDENCE[stage_of(inv)]
    d = landing(inv)
    if d is None:
        return 0.0          # no date at all cannot be planned around; it is still shown
    late = ((today or _today()) - d).days
    if late <= 0:
        return base
    for cap, factor in CONFIDENCE_STEPS:
        if late <= cap:
            return max(CONFIDENCE_FLOOR, round(base * factor, 4))
    return max(CONFIDENCE_FLOOR, round(base * CONFIDENCE_STEPS[-1][1], 4))


def _today() -> date:
    from .config import now
    return now().date()


def believable(items: list[dict], today: date | None = None) -> float:
    """The pending total after the stage and lateness haircuts — what a plan may lean on."""
    today = today or _today()
    return round(sum(float(i.get("amount") or 0) * confidence(i, today) for i in items), 2)


_CN_NUM = {"一": 1, "兩": 2, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7,
           "八": 8, "九": 9, "十": 10, "十一": 11, "十二": 12}


def _days_in_text(text: str) -> int:
    """「拍八天」 / 「8 天」 / 「5 days」 → the number of shoot days."""
    t = text or ""
    m = re.search(r"(\d+)\s*(?:天|days?\b)", t, re.I)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            return 0
    m = re.search(r"([一二兩三四五六七八九]|十[一二]?)\s*天", t)
    return _CN_NUM.get(m.group(1), 0) if m else 0


#: What Momo says she charges now. A pinned rate beats every inference, for the same reason
#: `expect_on` beats the 45-day model: she knows her own price, and averaging an old $200 gig
#: into it just makes the goal ask for days she will not actually need.
DAY_RATE_KEY = "cfg_day_rate"

#: How many recent jobs the observed figure is drawn from when nothing is pinned.
RATE_WINDOW = 3


def _rate_when(r: dict) -> str:
    """Best available date for ordering jobs newest-first."""
    for k in ("wrapped_on", "expect_on", "date", "when"):
        v = r.get(k)
        if v:
            return str(v)[:10]
    return ""


def _from_invoices(rows: list[dict]) -> list[dict]:
    """Her invoice archive, as jobs the rate can be measured from.

    Uses the CONTRACTED day figure — rate × days — not the invoice total. Prep fees and
    overtime are real income but they are not per-day, so dividing a total by shoot days
    inflates the answer: 「Woman in Blue Dress」 is $300/day contracted and $420/day if you
    divide $2,100 by five. The goal converts a dollar gap into days to book, so it needs
    the price of a day. Non-shoot work (a poster design) carries no days and drops out."""
    out = []
    for r in rows or []:
        try:
            d = int(r.get("days") or 0)
            rate = float(r.get("rate") or 0)
        except (TypeError, ValueError):
            continue
        if d <= 0 or rate <= 0:
            continue
        amt = round(float(r.get("day_total") or rate * d), 2)
        out.append({"amount": amt, "days": d, "rate": round(amt / d, 2),
                    "when": (r.get("date") or "")[:10],
                    "note": r.get("project") or r.get("client") or r.get("num") or ""})
    return out


def day_rate(items: list[dict], received: list[dict] | None = None,
             pinned: float | None = None, invoices: list[dict] | None = None) -> dict:
    """Her dollars-per-shoot-day, from jobs where the day count is known.

    Momo: "instead of estimating gig amount, have a algorithm to calculate my average day
    rate the past three months and use that number to calculate how many more work days I
    need." She has always said the days out loud — 「9/6-9/15，拍八天」 — and the system
    threw them away. A target in days is a target she can hold against a calendar.

    Work she has SHOT but not been paid for counts, on her instruction — "day rate i think
    we could also consider day rates of one's we shot but havent recieve the money yet."
    That work is the most recent evidence of what she charges; waiting 45 days for the
    cheque before believing it would keep the estimate permanently out of date.

    Three figures, because they answer different questions. ``rate`` is what the earning
    goal divides by — what a day she books TODAY will pay. ``observed`` is the day-weighted
    average across everything on record, which runs low whenever her price has been rising:
    hers reads about $327 against a going rate of $350, because a $200 job from June is
    still in the average. So the pinned figure wins when she has stated one, the most recent
    jobs win when she has not, and the long average is reported beside it rather than used.
    """
    # The archive is the authoritative record of what she charged and for how many days, so
    # when it is present it replaces the note-scraping entirely — otherwise a job appears
    # twice, once from its invoice and once from its pending row.
    inv_jobs = _from_invoices(invoices or [])
    rows = [] if inv_jobs else [*(items or []), *(received or [])]
    jobs = list(inv_jobs)
    for r in rows:
        try:
            d = int(r.get("days") or 0)
            a = float(r.get("amount") or 0)
        except (TypeError, ValueError):
            continue
        if not d:
            # she has been saying it in the note all along — 「9/6-9/15，拍八天」 — so read
            # it rather than wait for every old booking to be re-entered
            d = _days_in_text(f"{r.get('note') or ''} {r.get('merchant_desc') or ''}")
        if d > 0 and a > 0:
            jobs.append({"amount": round(a, 2), "days": d, "rate": round(a / d, 2),
                         "when": _rate_when(r),
                         "note": (r.get("note") or r.get("merchant_desc") or "")[:40]})

    if not jobs:
        return {"rate": round(float(pinned), 2) if pinned else 0.0,
                "n": 0, "days": 0, "total": 0.0, "observed": 0.0, "recent": 0.0,
                "source": "pinned" if pinned else "none", "jobs": []}

    jobs.sort(key=lambda j: j["when"] or "", reverse=True)
    total = sum(j["amount"] for j in jobs)
    days = sum(j["days"] for j in jobs)
    observed = round(total / days, 2)

    win = jobs[:RATE_WINDOW]
    recent = round(sum(j["amount"] for j in win) / sum(j["days"] for j in win), 2)

    if pinned:
        rate, source = round(float(pinned), 2), "pinned"
    else:
        rate, source = recent, "recent"
    return {"rate": rate, "source": source, "n": len(jobs), "days": days,
            "total": round(total, 2), "observed": observed, "recent": recent,
            "window": len(win), "jobs": jobs[:8]}


async def pinned_day_rate(session) -> float | None:
    raw = await get_kv(session, DAY_RATE_KEY)
    try:
        v = float(raw) if raw else 0.0
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


async def set_pinned_day_rate(session, amount: float) -> float:
    v = max(0.0, float(amount))
    await set_kv(session, DAY_RATE_KEY, str(round(v, 2)))
    return v


async def pending_invoices(session) -> list:
    """Expected freelance payments Momo is still waiting on (not yet marked received)."""
    prof = await get_income_profile(session)
    return [u for u in prof["upcoming"] if (u.get("status") or "pending") != "received"]


def _find_invoice(items: list, which) -> dict | None:
    """Match a pending payment by name first, then by amount."""
    key = _norm(which)
    if key:
        for u in items:
            n = _norm(u.get("note"))
            if n and (key in n or n in key):
                return u
    try:
        amt = float(re.sub(r"[^0-9.]", "", str(which)))
    except (TypeError, ValueError):
        amt = None
    if amt:
        for u in items:
            if abs(float(u.get("amount") or 0) - amt) < 0.5:
                return u
    return None


async def update_invoice(session, which, amount=None, when=None, note=None) -> dict | None:
    """Change an existing expected payment — the amount moved, the date slipped.

    This did not exist, so "Avia 從 2800 變成 2850" had nowhere to go: the parser only
    knew 'received' and 'add', the message fell through to free-text Q&A, and she
    cheerfully said she'd updated it. Saying it and doing it are now the same code path."""
    items = _load_list(await get_kv(session, "cfg_upcoming"))
    hit = _find_invoice(items, which)
    if hit is None:
        return None
    if amount is not None:
        hit["amount"] = float(amount)
    if when:
        hit["when"] = when
    if note:
        hit["note"] = note
    await set_kv(session, "cfg_upcoming", json.dumps(items))
    return hit


async def mark_invoice(session, which, status="received") -> dict | None:
    """Flip a pending invoice's status (e.g. it finally landed)."""
    items = _load_list(await get_kv(session, "cfg_upcoming"))
    hit = _find_invoice(items, which)
    if hit is None:
        return None
    hit["status"] = status
    await set_kv(session, "cfg_upcoming", json.dumps(items))
    return hit


async def add_invoice(session, amount, when=None, note=None, *, stage=None,
                      wrapped_on=None, days=None, expect_on=None) -> dict:
    """Record a new expected payment Momo just booked."""
    items = _load_list(await get_kv(session, "cfg_upcoming"))
    item = {"amount": float(amount), "when": when, "note": note, "status": "pending"}
    if stage in STAGE_CONFIDENCE:
        item["stage"] = stage
    if wrapped_on:
        item["wrapped_on"] = str(wrapped_on)[:10]
        item.setdefault("stage", "wrapped")
    if expect_on:
        item["expect_on"] = str(expect_on)[:10]
    if days:
        try:
            item["days"] = int(days)
        except (TypeError, ValueError):
            pass
    items.append(item)
    await set_kv(session, "cfg_upcoming", json.dumps(items))
    return item


async def update_account(session, name, amount, typ=None) -> dict:
    """Set the current balance of one named account (e.g. Apple Card), matching by name.
    Adds it if she hasn't heard of it. Keeps the cash/debt totals in sync."""
    accts = _load_list(await get_kv(session, "cfg_accounts"))
    key = _norm(name)
    hit = None
    for a in accts:
        if _norm(a.get("name")) == key:
            hit = a
            break
    if hit is None:
        hit = {"name": name, "type": typ or "cash", "amount": 0.0}
        accts.append(hit)
        added = True
    else:
        added = False
    hit["amount"] = float(amount)
    if typ in ("cash", "credit"):
        hit["type"] = typ
    await set_kv(session, "cfg_accounts", json.dumps(accts))
    await _refresh_totals(session, accts)
    return {"name": hit["name"], "amount": float(amount), "type": hit["type"], "added": added}

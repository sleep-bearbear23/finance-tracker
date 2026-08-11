"""A quarter you can score, instead of a target that silently absorbs your work.

Momo: "say I saw her advice earning amount and I went out there sourcing for work. I come
back successfully secured a job in October that pays me 2k, and I told her about this. I
wanna be able to see my success reflected on this block, showing me what was adviced of me
and what progress did securing that gig get me."

The 需要賺 card could not do that. It recomputed from scratch on every load, so booking a
$2,000 gig made the floor sit exactly where it was and the conditional line quietly drop —
the card had no memory that it had ever asked for anything, and no memory that she went
and did it. Effort vanished into arithmetic.

A season fixes the target at a moment in time and then measures against that fixed thing:

  target    the three tiers as they were the day the season opened, frozen
  landed    real income that has arrived since, from the ledger
  booked    work she has since taken, at its confidence weight
  events    a dated log of what moved the number, including the money that was already
            on the books at kickoff — that is her starting position, not her achievement

The target only moves when it is *made* to move (her costs change, her spending drifts),
and when it does that is its own event on the log — 「目標變了，不是你退步」. Without that
line a rising cost of living reads as backsliding, which is the difference between a
scoreboard and a treadmill.
"""
from __future__ import annotations

import json
from datetime import date, timedelta

from . import budget
from . import facts as F
from . import prefs
from .config import now
from .db import get_kv, set_kv

KEY = "cfg_season"

#: A rolling three months has no finish line — the start date walks forward with today, so
#: there is never a moment where she finds out how the quarter went. Momo: "we keep the
#: season date align with tax seasons, so I can actually see how much money I actually
#: received this season, how much I'm expecting, and how much more I need before a fixed
#: end date."
#:
#: The boundaries are the IRS estimated-tax periods, which are NOT calendar quarters —
#: Jun–Aug, Sep–Dec, Jan–Mar, Apr–May. Uneven on purpose, and worth it: the season's income
#: is exactly the income the next payment is assessed on, so 「這一季賺了多少」 and 「9/15 要繳
#: 多少」 are finally the same question. Targets scale with the period's real length.
SEASON_DAYS = 92          # only a fallback, if the tax table runs out

#: Momo: "goals and estimated income need can refresh every week." Frozen forever goes
#: stale; live tracking turns a bad fortnight into apparent backsliding. Weekly is
#: predictable, and every move is written to the log as its own event.
REFRESH_DAYS = 7


def _months(lo: date, hi: date) -> float:
    return ((hi - lo).days + 1) / 30.4


def _scale(lo: date, hi: date) -> float:
    """The tiers are computed over a three-month horizon; a season may be 2 or 4."""
    from .analytics import HORIZON_MONTHS
    return _months(lo, hi) / HORIZON_MONTHS


async def get(session) -> dict | None:
    raw = await get_kv(session, KEY)
    try:
        d = json.loads(raw) if raw else None
    except ValueError:
        return None
    return d if isinstance(d, dict) else None


def bounds(today: date) -> tuple[date, date, dict | None]:
    """The tax period today sits in, and the payment it feeds."""
    from . import tax as TAX
    for d in TAX.DEADLINES:
        try:
            lo = date.fromisoformat(d["covers"][0])
            hi = date.fromisoformat(d["covers"][1])
        except (ValueError, KeyError, IndexError):
            continue
        if lo <= today <= hi:
            return lo, hi, d
    # past the end of the table: fall back to a plain quarter so nothing breaks
    return today, today + timedelta(days=SEASON_DAYS - 1), None


async def start(session, tiers: list[dict], today: date | None = None,
                days: int = SEASON_DAYS) -> dict:
    """Open a season and freeze what it is asking for."""
    today = today or now().date()
    lo, hi, dl = bounds(today)
    pend = await prefs.pending_invoices(session)
    s = {
        "start": lo.isoformat(),
        "end": hi.isoformat(),
        "refreshed": today.isoformat(),
        "tax": ({"due": dl["due"], "label": dl["label"]} if dl else None),
        "months": round(_months(lo, hi), 2),
        # The tax periods are 2, 3 and 4 months long. A target computed for three months
        # would ask too little of a four-month season and too much of a two-month one.
        "targets": {t["name"]: round(t["bare"] * _scale(lo, hi), 2) for t in tiers},
        "opened_with": {                       # her position at kickoff, for honest framing
            "booked_face": round(sum(float(p.get("amount") or 0) for p in pend), 2),
            "booked_weighted": prefs.believable(pend, today),
            "n": len(pend),
            # exactly which jobs she walked in holding. Without this, restarting a season
            # on the same day work was booked would re-credit that work as this season's
            # achievement — the scoreboard has to know the difference between the starting
            # position and the score.
            "notes": [prefs._norm(p.get("note") or "") for p in pend],
        },
        "rebases": [],
    }
    await set_kv(session, KEY, json.dumps(s, ensure_ascii=False))
    return s


async def ensure(session, tiers: list[dict]) -> dict:
    """The season Momo is in, opening one on first sight rather than asking her to."""
    s = await get(session)
    today = now().date()
    lo, hi, _ = bounds(today)
    # a new tax period, or a season from before the boundaries were tax-aligned
    if s is None or str(s.get("end") or "") != hi.isoformat() \
            or str(s.get("start") or "") != lo.isoformat():
        return await start(session, tiers, today)
    return await _maybe_rebase(session, s, tiers, today)


async def _maybe_rebase(session, s: dict, tiers: list[dict], today: date) -> dict:
    """Recompute the target on a weekly beat, and say so out loud when it moves.

    A target that tracked her spending continuously would turn a bad fortnight into
    apparent backsliding; one that never moved would be lying by the end of the quarter.
    Weekly is a cadence she can feel, and every move lands on the log as 「目標變了，不是你
    退步」 rather than as a number that quietly changed while she was not looking."""
    last = str(s.get("refreshed") or s.get("start") or "")
    try:
        due = (today - date.fromisoformat(last)).days >= REFRESH_DAYS
    except ValueError:
        due = True
    if not due:
        return s
    try:
        k = _scale(date.fromisoformat(s["start"]), date.fromisoformat(s["end"]))
    except (ValueError, KeyError):
        k = 1.0
    live = {t["name"]: round(t["bare"] * k, 2) for t in tiers}
    old = s.get("targets") or {}
    moved = {k: (old.get(k), v) for k, v in live.items()
             if old.get(k) and abs(v - old[k]) >= 1.0}
    s["refreshed"] = today.isoformat()
    if not moved:
        await set_kv(session, KEY, json.dumps(s, ensure_ascii=False))
        return s
    s.setdefault("rebases", []).append({
        "at": today.isoformat(),
        "changes": [{"tier": k, "from": a, "to": b} for k, (a, b) in moved.items()],
    })
    s["targets"] = live
    await set_kv(session, KEY, json.dumps(s, ensure_ascii=False))
    return s


async def progress(session, tiers: list[dict], f: F.Facts | None = None) -> dict:
    """Where the season stands, and the dated list of what got it there."""
    s = await ensure(session, tiers)
    f = f or await F.build(session)
    today = now().date()
    lo = date.fromisoformat(s["start"])
    hi = date.fromisoformat(s["end"])

    # money that has actually arrived inside the season
    landed, landed_rows = 0.0, []
    for t in f.txns:
        if not budget.is_income(t):
            continue
        d = budget.eff_date(t)
        if not d or d < lo or d > hi:
            continue
        landed += t.amount
        landed_rows.append({"date": d.isoformat(), "label": (t.merchant_desc or "")[:40],
                            "amount": round(t.amount, 2), "kind": "landed"})

    # work she is holding that should land inside the season, dated by the day she got it
    won = await _won_dates(session)
    carried = set((s.get("opened_with") or {}).get("notes") or [])
    booked_face = booked = 0.0
    booked_rows = []
    for p in await prefs.pending_invoices(session):
        land = prefs.landing(p)
        if land is None or not _counts(p, land, lo, hi):
            continue
        amt = float(p.get("amount") or 0)
        conf = prefs.confidence(p, today)
        booked_face += amt
        booked += amt * conf
        key = prefs._norm(p.get("note") or "")
        got = won.get(key)
        during = bool(got and lo.isoformat() <= got <= hi.isoformat()
                      and key not in carried)
        booked_rows.append({"date": got if during else lo.isoformat(),
                            "label": (p.get("note") or "某案")[:40],
                            "amount": round(amt, 2), "weighted": round(amt * conf, 2),
                            "confidence": conf, "kind": "booked",
                            "mine": during,     # secured during the season vs. carried in
                            "lands": land.isoformat(),
                            "lands_after": land > hi,   # worked in season, paid just after
                            "when": p.get("when"),
                            "late_days": max(0, (today - land).days)})

    secured = round(landed + booked, 2)
    rows = []
    for name, bare in (s.get("targets") or {}).items():
        rows.append({
            "name": name, "target": bare,
            "remaining_floor": round(max(0.0, bare - landed), 2),
            "remaining": round(max(0.0, bare - secured), 2),
            "pct": round(min(100.0, 100 * secured / bare), 1) if bare else 0.0,
            "pct_landed": round(min(100.0, 100 * landed / bare), 1) if bare else 0.0,
            "cleared": secured >= bare,
        })

    # One row per dollar of `secured`, so the log's running total ends exactly on the
    # headline. Ordered by the day she got it — booked work by the day she reported it,
    # arrivals by the day the money showed up.
    events = sorted(landed_rows + booked_rows, key=lambda r: (r["date"], r["kind"]))
    run = 0.0
    hold = (s.get("targets") or {}).get("持平") or 0.0
    for e in events:
        run += e.get("weighted", e["amount"])
        e["running"] = round(run, 2)
        e["remaining"] = round(max(0.0, hold - run), 2) if hold else None

    elapsed = (today - lo).days + 1
    total_days = (hi - lo).days + 1
    return {
        "start": s["start"], "end": s["end"],
        "days_elapsed": max(0, elapsed), "days_left": max(0, (hi - today).days),
        "pace": round(100 * elapsed / total_days, 1) if total_days else 0.0,
        "landed": round(landed, 2),
        "booked": round(booked, 2), "booked_face": round(booked_face, 2),
        "secured": secured,
        "won_this_season": round(sum(e["weighted"] for e in booked_rows if e["mine"]), 2),
        "opened_with": s.get("opened_with") or {},
        "tiers": rows,
        "rebases": s.get("rebases") or [],
        "events": events,
    }


def _counts(inv: dict, land: date, lo: date, hi: date) -> bool:
    """Does this booking belong to this season?

    Two things are true at once and a single rule cannot hold both. The TARGET is a cash
    need — three months of living costs — so money arriving in the window counts. But the
    SCOREBOARD is about effort, and a job worked in October that pays on 11/14 is work she
    went out and won during the season, four days of calendar away from being invisible.
    A strict cash rule dropped exactly that job on the floor.

    So: it counts if the money lands inside the window, OR if the work itself was done
    inside it. Only work performed after the season AND paid after it is somebody else's
    quarter."""
    if lo <= land <= hi:
        return True
    w = str(inv.get("when") or "")[:7]
    try:
        y, m = int(w[:4]), int(w[5:7])
    except (ValueError, IndexError):
        return False
    w_start = date(y, m, 1)
    w_end = date(y + (m // 12), (m % 12) + 1, 1) - timedelta(days=1)
    return w_start <= hi and w_end >= lo          # the work month overlaps the season


async def _won_dates(session) -> dict[str, str]:
    """When each booking was first reported, out of the change log.

    This is what turns the card into a scoreboard: without it every booking is dated by
    when the money is expected, and "I went out and found this in October" is invisible."""
    from sqlalchemy import select

    from .models import Change
    rows = (await session.execute(
        select(Change).where(Change.tool == "add_expected_payment",
                             Change.undone_at.is_(None))
        .order_by(Change.at))).scalars().all()
    out: dict[str, str] = {}
    for c in rows:
        try:
            args = json.loads(c.args or "{}")
        except ValueError:
            continue
        key = prefs._norm(str(args.get("note") or ""))
        if key and key not in out and c.at:
            out[key] = c.at.date().isoformat()
    return out

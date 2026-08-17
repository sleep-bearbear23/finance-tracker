"""期末結算 — the settlement ritual.

Momo asked for closure and initiation to be events, not silent rollovers: at the end of
a session (her word for a half-month period) she wants the numbers reported and to decide
herself where the remainder goes; at the start of the next one she wants a heading, keyed
to how the quarter goal is actually tracking.

The rule that makes it real: **a period that hasn't been settled doesn't get a budget.**
That isn't an artificial gate — allocation moves money into jars, and jars set the water,
so the next line is genuinely undefined until she decides. What it must NOT block is data:
the bank keeps syncing, she keeps asking about charges, Momo keeps logging. A five-day
shoot can cost her a budget; it must never cost her the record.

Batch A covers the session boundary. The quarter's fuller meeting — allocation across
every jar, the next quarter's objective — is Batch B; the storage here already holds
its shape.
"""
from __future__ import annotations

import json
from datetime import date, timedelta

from sqlalchemy import select

from . import budget
from . import period as P
from .config import now
from .db import get_kv, set_kv
from .models import Settlement

#: The first period that REQUIRES settling. Stamped the first time this runs, so the
#: feature never reaches backwards and freezes months Momo was never asked about.
FROM_KEY = "cfg_settle_from"

#: Set when /api/export runs, so 「上次備份 X 天前」 is measured rather than guessed.
EXPORT_KEY = "last_export"

BACKUP_STALE_DAYS = 7

#: Where the file goes. Naming it every time is the whole point — a reminder that needs
#: re-reading is a reminder that gets ignored.
BACKUP_FOLDER = "Art Path/1_CAREER/fin/_archive/"

REFLECTION = [
    ("worth", "這一期最值得的一筆是什麼？"),
    ("regret", "有沒有哪一筆，現在想起來覺得不值？"),
    ("change", "下一期想調整什麼？（一件就好）"),
]


async def settle_from(session) -> str:
    """The first period key that needs a settlement; stamps itself on first call."""
    got = await get_kv(session, FROM_KEY)
    if got:
        return got
    key = budget.current_key()
    await set_kv(session, FROM_KEY, key)
    return key


ARMED_KEY = "settle_armed"


async def arm(session, key: str) -> None:
    """Momo said yes in LINE. That doesn't perform anything — it just means the survey
    should open by itself next time she loads the dashboard, on the machine where typing
    three answers isn't a chore."""
    await set_kv(session, ARMED_KEY, key)


async def is_armed(session, key: str) -> bool:
    return (await get_kv(session, ARMED_KEY)) == key


async def disarm(session) -> None:
    await set_kv(session, ARMED_KEY, "")


async def get_one(session, key: str) -> Settlement | None:
    return (await session.execute(
        select(Settlement).where(Settlement.period_key == key))).scalars().first()


async def unsettled(session, today: date | None = None) -> list[str]:
    """Ended periods, at or after the grace point, with no settlement row. Oldest first."""
    today = today or now().date()
    start = await settle_from(session)
    cur = budget.current_key()
    out = []
    for k in P.series(start, cur):
        if k == cur:
            break                       # the period we're standing in hasn't ended
        _, hi = P.key_bounds(k)
        if hi >= today:
            continue
        if await get_one(session, k) is None:
            out.append(k)
    return out


async def state(session, today: date | None = None) -> dict:
    """What every surface needs to know: is a budget owed a meeting first?"""
    pending = await unsettled(session, today)
    return {
        "awaiting": bool(pending),
        "periods": pending,
        "oldest": pending[0] if pending else None,
        "label": P.label(pending[0]) if pending else None,
    }


async def record(session, key: str, *, pocket: float, destination: str,
                 reflection: dict | None = None, kind: str = "session",
                 allocations: dict | None = None, objective: dict | None = None,
                 note: str | None = None) -> Settlement:
    """Write the settlement for one period. One row per period, ever.

    Idempotent on purpose: ``close_session`` could be called twice and double-credit the
    season pot, which is exactly the class of bug a ritual with a submit button invites.
    """
    existing = await get_one(session, key)
    if existing is not None:
        return existing
    row = Settlement(
        period_key=key, kind=kind, at=now(), pocket=round(float(pocket or 0.0), 2),
        destination=destination or "carry",
        reflection=json.dumps(reflection or {}, ensure_ascii=False),
        allocations=json.dumps(allocations or {}, ensure_ascii=False),
        objective=json.dumps(objective if objective is not None else [], ensure_ascii=False),
        note=note,
    )
    session.add(row)
    await session.commit()
    return row


async def last_reflection(session, before_key: str | None = None) -> dict | None:
    """Her own words from last time, so the next page can hand them back to her."""
    rows = (await session.execute(
        select(Settlement).order_by(Settlement.at.desc()))).scalars().all()
    for r in rows:
        if before_key and r.period_key >= before_key:
            continue
        try:
            data = json.loads(r.reflection or "{}")
        except (TypeError, ValueError):
            data = {}
        if any(v for v in data.values()):
            return {"period": r.period_key, "label": P.label(r.period_key), "answers": data}
    return None


# ── the quarter ──────────────────────────────────────────────────────────────
#
# A season is an IRS estimated-tax period (season.bounds), so 「這一季賺多少」 and
# 「9/15 要繳多少」 are the same question. Quarter settlements are namespaced so they
# can share the settlements table with session ones without colliding.

QKEY = "Q:"


def quarter_key(end: date) -> str:
    return f"{QKEY}{end.isoformat()}"


def prev_quarter(today: date | None = None) -> tuple[date, date, str] | None:
    """The season that has ENDED and is therefore owed a settlement, if any."""
    from . import season as SE
    today = today or now().date()
    lo, hi, _ = SE.bounds(today)
    if today <= hi:
        # standing inside the season: the one before it is the candidate
        prev_end = lo - timedelta(days=1)
        plo, phi, _ = SE.bounds(prev_end)
        return (plo, phi, quarter_key(phi)) if phi < today else None
    return lo, hi, quarter_key(hi)


async def quarter_pending(session, today: date | None = None) -> dict | None:
    """Is a finished season waiting to be closed? Honours the same grace point as
    sessions — a season that ended before the feature existed is not chased."""
    got = prev_quarter(today)
    if not got:
        return None
    lo, hi, key = got
    start_key = await settle_from(session)
    try:
        if hi < P.key_bounds(start_key)[0]:
            return None
    except Exception:
        pass
    if await get_one(session, key) is not None:
        return None
    return {"start": lo.isoformat(), "end": hi.isoformat(), "key": key}


#: Filled in safety order, not size order: the small buffer that stops a surprise from
#: eating the grocery money comes before the large fund that only matters in a drought.
FILL_ORDER = ("contingency", "dmv", "car", "floor", "emergency", "experiment")


async def spare(session) -> dict:
    """What there is to allocate at a quarter close.

    Two sources, deliberately kept apart: the 季目標 pot, which is money Momo already
    decided to set aside session by session, and the free water, which is her operating
    cash. The proposal spends the pot and leaves the water alone — locking away her
    grocery money without being asked is exactly the kind of helpfulness nobody wants.
    """
    from . import allowance as AL
    from . import jars as J
    a = await AL.compute(session)
    js = await J.load(session)
    pot = float((J.get(js, "season") or {}).get("balance") or 0.0)
    water = max(0.0, float(a.get("available") or 0.0))
    return {"pot": round(pot, 2), "water": round(water, 2),
            "total": round(pot + water, 2), "jars": js}


def propose(pot: float, jars: list[dict]) -> list[dict]:
    """Fill the jars that are furthest from doing their job, in safety order.

    Returns one row per jar with the suggested amount — every row editable on the page,
    because this is a proposal, not a verdict.
    """
    left = round(max(0.0, pot), 2)
    out = []
    by_id = {j.get("id"): j for j in jars}
    for jid in FILL_ORDER:
        j = by_id.get(jid)
        if not j:
            continue
        bal = float(j.get("balance") or 0.0)
        tgt = j.get("target")
        gap = round(float(tgt) - bal, 2) if tgt else 0.0
        give = round(min(left, gap), 2) if gap > 0 else 0.0
        out.append({"id": jid, "name": j.get("name") or jid, "balance": round(bal, 2),
                    "target": round(float(tgt), 2) if tgt else None,
                    "gap": max(0.0, gap), "suggest": give})
        left = round(left - give, 2)
    return out


# ── the next quarter's objective ─────────────────────────────────────────────

async def objective(session) -> dict | None:
    """One objective for the quarter ahead: the TYPE from the engine's own diagnosis,
    the SIZE from what that lever can actually move in the days that remain.

    Momo's rule, and the reason this exists: chasing $10k was a good quarter task
    *because* only ~18 days were left — an objective the remaining days can't move is
    a wish with a number on it.
    """
    from . import allowance as AL
    from . import analytics as AN
    from . import prefs as PR
    from . import season as SE
    today = now().date()
    lo, hi, _ = SE.bounds(today)
    days_left = max(1, (hi - today).days)
    a = await AL.compute(session)
    binding = a.get("binding") or ""
    kind = a.get("deficit_kind")

    # timing / 水位 → the money is already earned, it just hasn't arrived
    if kind == "timing" or binding == "水位":
        pend = await PR.pending_invoices(session)
        chaseable = 0.0
        oldest = None
        for u in pend:
            land = PR.landing(u)
            if land and land <= today + timedelta(days=days_left):
                chaseable += float(u.get("amount") or 0)
                if oldest is None or (land and land < oldest):
                    oldest = land
        if chaseable > 0:
            return {"type": "chase", "amount": round(chaseable, 2), "days": days_left,
                    "text": f"這一季把該進來的 ${chaseable:,.0f} 催回來",
                    "why": f"錢是賺到了，只是還沒到；剩 {days_left} 天，催得動的就是這些。"}

    # spending / 軌跡 → her recent pace is the constraint
    if binding == "軌跡":
        med = float(a.get("recent_median") or 0)
        line = float(a.get("allowance") or 0)
        if med > line > 0:
            per = round(med - line, 2)
            periods = max(1, round(days_left / 15))
            return {"type": "trim", "amount": round(per * periods, 2), "days": days_left,
                    "text": f"這一季每期少花 ${per:,.0f}（總共 ${per * periods:,.0f}）",
                    "why": f"最近每期花 ${med:,.0f}，線是 ${line:,.0f}，差的就是這些。"}

    # otherwise → book work, sized by what fits in the calendar
    try:
        te = await AN.to_earn(session, 3)
        tiers = te.get("tiers") or []
        tier = next((t for t in tiers if t.get("name") == "持平"), tiers[0] if tiers else None)
        rate = float(te.get("day_rate", {}).get("rate") or 0) if isinstance(
            te.get("day_rate"), dict) else 0.0
        need_days = float(tier.get("work_days") or 0) if tier else 0.0
        if need_days > 0:
            fits = min(need_days, days_left)
            return {"type": "book", "amount": round(fits * rate, 2) if rate else None,
                    "days": days_left, "book_days": round(fits, 1),
                    "text": f"這一季再接 {fits:,.0f} 天",
                    "why": (f"要打平還差 {need_days:,.0f} 天，剩 {days_left} 天可以排；"
                            + ("排得下。" if need_days <= days_left
                               else "排不完的部分要往下一季推，或者調價。"))}
    except Exception as e:
        print(f"[objective] {e!r}")
    return None


async def fund_options(session) -> list[dict]:
    """Goal jars Momo could aim at this quarter. A jar is an intention until she picks
    it here — this is the only place a savings goal turns into an obligation, and she is
    the one who does the turning."""
    from . import jars as J
    out = []
    for j in await J.load(session):
        if j.get("kind") not in ("goal", "experiment") or not j.get("target"):
            continue
        tgt = float(j["target"])
        bal = float(j.get("balance") or 0.0)
        row = {"id": j["id"], "name": j.get("name") or j["id"],
               "target": round(tgt, 2), "balance": round(bal, 2),
               "pct_now": round(100 * bal / tgt, 1) if tgt else 0.0,
               "by_date": j.get("by_date")}
        adv = J.rate_advice(j)
        if adv:
            row["advice"] = adv["text"]
            row["per_period"] = adv["per_period"]
        out.append(row)
    return out


def fund_objective(jar: dict, pct: float) -> dict:
    """「這一季補到 70%」 — a percentage of the FULL target, with the remaining gap
    spelled out so the number she is agreeing to is the number she has to find."""
    tgt = float(jar.get("target") or 0)
    bal = float(jar.get("balance") or 0)
    want = round(tgt * (pct / 100.0), 2)
    add = round(max(0.0, want - bal), 2)
    return {"type": "fund", "jar": jar.get("id"), "jar_name": jar.get("name"),
            "pct": pct, "target_balance": want, "add": add,
            "text": f"這一季把「{jar.get('name')}」補到 {pct:.0f}%（${want:,.0f}）",
            "why": (f"目標 ${tgt:,.0f} 的 {pct:.0f}% 是 ${want:,.0f}；"
                    f"罐裡已經有 ${bal:,.0f}，所以這一季要再放 ${add:,.0f}。")}


# ── did last quarter's objectives happen? ────────────────────────────────────

def _verdict(done: float, need: float) -> str:
    if need <= 0:
        return "hit"
    r = done / need
    return "hit" if r >= 0.995 else ("partial" if r >= 0.5 else "missed")


async def score(session, objs: list[dict], lo: date, hi: date) -> list[dict]:
    """Measure each objective against what actually happened.

    Where the data can't answer, the verdict is ``unknown`` and says why — Rule 2 applies
    to grading as much as to forecasting. An objective marked 「做到了」 on a guess is
    worse than one left open.
    """
    from . import jars as J
    out = []
    js = await J.load(session)
    for o in objs or []:
        row = {**o, "verdict": "unknown", "detail": ""}
        t = o.get("type")
        try:
            if t == "fund":
                jar = J.get(js, o.get("jar")) or {}
                bal = float(jar.get("balance") or 0.0)
                need = float(o.get("target_balance") or 0.0)
                row["verdict"] = _verdict(bal, need)
                row["detail"] = f"罐裡現在 ${bal:,.0f}，目標是 ${need:,.0f}"
            elif t == "chase":
                from . import budget as B
                from .models import Transaction
                rows = (await session.execute(select(Transaction))).scalars().all()
                got = sum(t_.amount for t_ in rows
                          if B.is_income(t_) and (d := B.eff_date(t_)) and lo <= d <= hi)
                need = float(o.get("amount") or 0.0)
                row["verdict"] = _verdict(got, need)
                row["detail"] = f"這一季實際進帳 ${got:,.0f}，目標是催回 ${need:,.0f}"
            elif t == "book":
                row["detail"] = "接了幾天要看案子紀錄，這裡先不打分數"
            elif t == "trim":
                row["detail"] = "花費的比較要跨期算，這裡先不打分數"
        except Exception as e:                       # never let grading break the page
            row["detail"] = f"算不出來（{type(e).__name__}）"
        out.append(row)
    return out


async def last_quarter_objectives(session) -> dict | None:
    """The previous quarter's objectives, scored, for the page to show as a record."""
    rows = (await session.execute(
        select(Settlement).where(Settlement.kind == "quarter")
        .order_by(Settlement.at.desc()))).scalars().all()
    for r in rows:
        try:
            objs = json.loads(r.objective or "[]")
        except (TypeError, ValueError):
            objs = []
        if isinstance(objs, dict):
            objs = [objs]
        if not objs:
            continue
        end = r.period_key[len(QKEY):] if r.period_key.startswith(QKEY) else None
        try:
            hi = date.fromisoformat(end) if end else now().date()
        except (TypeError, ValueError):
            hi = now().date()
        lo = hi - timedelta(days=92)
        return {"key": r.period_key, "scored": await score(session, objs, lo, hi)}
    return None


# ── the generated reflection question ────────────────────────────────────────

async def noticed_question(session, key: str) -> str | None:
    """One question the data earned — the auntie move, and the part no template writes.

    Deliberately about a life rather than a budget: fourteen meals out is an observation
    about a fortnight that was probably too busy to cook, and that is the doorway. Returns
    None when nothing stands out, because inventing a pattern is worse than asking nothing.
    """
    from .models import Transaction
    lo, hi = P.key_bounds(key)
    rows = (await session.execute(select(Transaction))).scalars().all()
    eat_out = 0
    cats: dict[str, float] = {}
    biggest = None
    for t in rows:
        d = budget.eff_date(t)
        if not d or not (lo <= d <= hi) or not budget.is_spend(t):
            continue
        amt = budget.spend_amount(t)
        cats[t.category or "未分類"] = cats.get(t.category or "未分類", 0.0) + amt
        if amt > 0 and (biggest is None or amt > biggest[1]):
            biggest = (t.merchant_desc or "那筆", amt)
        desc = (t.merchant_desc or "").lower()
        if t.category in ("food", "snacks") and any(
                w in desc for w in ("uber", "doordash", "grubhub", "postmates",
                                    "hungrypanda", "eats", "restaurant", "kitchen")):
            eat_out += 1
    if eat_out >= 8:
        return f"這一期外食 {eat_out} 次，是不是很忙？"
    if biggest and biggest[1] >= 200:
        return f"這一期最大的一筆是 {biggest[0]} ${biggest[1]:,.0f}，值得嗎？"
    if cats:
        top = max(cats.items(), key=lambda kv: kv[1])
        from . import taxonomy as T
        if top[1] >= 150:
            return f"這一期花最多的是「{T.label(top[0])}」${top[1]:,.0f}，跟你想的一樣嗎？"
    return None


# ── backup ───────────────────────────────────────────────────────────────────

async def stamp_export(session) -> None:
    await set_kv(session, EXPORT_KEY, now().date().isoformat())


async def backup_state(session) -> dict:
    raw = await get_kv(session, EXPORT_KEY)
    try:
        last = date.fromisoformat(raw) if raw else None
    except (TypeError, ValueError):
        last = None
    days = (now().date() - last).days if last else None
    return {"last": last.isoformat() if last else None, "days": days,
            "stale": days is None or days >= BACKUP_STALE_DAYS,
            "folder": BACKUP_FOLDER}


def backup_message(bs: dict, base_url: str) -> str:
    """Always the same words. Momo asked for the instruction to be clear every time so
    it never becomes a thing to figure out."""
    when = (f"上次備份是 {bs['days']} 天前（{bs['last']}）" if bs.get("last")
            else "還沒備份過")
    return (f"默默，每週備份時間。{when}。\n"
            f"1. 點這個：{base_url}/api/export\n"
            f"2. 存到 {BACKUP_FOLDER}\n"
            "檔案就是你全部的帳、商家記憶跟罐子。Railway 上那份不算備份，"
            "要留一份在你自己電腦裡。")


# ── the two messages around the boundary ─────────────────────────────────────

def close_notice(clo: dict, base_url: str) -> str:
    """The notice ASKS; it doesn't hand over a form.

    Momo found the survey much easier at a keyboard than on her phone — three free-text
    answers and an editable allocation is not thumb work. So LINE does what LINE is good
    at (noticing, and getting a yes) and the dashboard does the form.
    """
    pocket = clo.get("pool") or 0.0
    head = (f"{clo['label']} 結束了。守住 {clo['days_under']} 天、超過 {clo['days_over']} 天，"
            f"口袋 ${pocket:,.0f}。")
    return (head + "\n要結算嗎？說一聲我就開起來，到電腦上打開儀表板就會自己跳出來。"
            "\n（結算完才會開下一期的額度。帳我照記，不用擔心。）")


async def current_objectives(session) -> list[dict]:
    """This quarter's objectives, primary first. Stored as a list since Momo asked for
    one 主目標 plus a couple of 次目標 — importance stated up front rather than inferred
    later from which one she happened to chase."""
    rows = (await session.execute(
        select(Settlement).where(Settlement.kind == "quarter")
        .order_by(Settlement.at.desc()))).scalars().all()
    for r in rows:
        try:
            objs = json.loads(r.objective or "[]")
        except (TypeError, ValueError):
            objs = []
        if isinstance(objs, dict):                     # pre-list settlements
            objs = [objs] if objs.get("text") else []
        objs = [o for o in objs if o.get("text")]
        if objs:
            return sorted(objs, key=lambda o: 0 if o.get("rank") == "primary" else 1)
    return []


async def current_objective(session) -> dict | None:
    objs = await current_objectives(session)
    return objs[0] if objs else None


async def open_message(session, key: str, quarter: dict | None = None) -> str:
    """The heading for a new period, keyed to how the quarter is actually tracking."""
    from . import allowance as AL
    a = await AL.compute(session, key)
    line = a.get("allowance") or 0.0
    days = a.get("days_left") or P.days_in(key)
    per_day = round(line / days, 0) if days else 0
    out = [f"{P.label(key)} 開始了，這期的線 ${line:,.0f}（一天大概 ${per_day:,.0f}）。"]
    if quarter and quarter.get("target"):
        got, tgt = quarter.get("secured") or 0.0, quarter["target"]
        gap = max(0.0, tgt - got)
        if gap <= 0:
            out.append(f"這一季的目標 ${tgt:,.0f} 已經到了，剩下的是加分。")
        else:
            out.append(f"這一季還差 ${gap:,.0f}"
                       + (f"（大概 {quarter['days_needed']:.0f} 天工作）"
                          if quarter.get("days_needed") else "")
                       + "。")
    objs = await current_objectives(session)
    if objs:
        out.append(f"這一季你自己定的主目標：{objs[0]['text']}。")
        rest = [o["text"] for o in objs[1:]]
        if rest:
            out.append("次要的：" + "、".join(rest) + "。")
    return "\n".join(out)

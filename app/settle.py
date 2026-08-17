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
        objective=json.dumps(objective or {}, ensure_ascii=False),
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
    pocket = clo.get("pool") or 0.0
    head = (f"{clo['label']} 結束了。守住 {clo['days_under']} 天、超過 {clo['days_over']} 天，"
            f"口袋 ${pocket:,.0f}。")
    return (head + "\n來結算一下，決定這筆錢去哪、順便回顧兩句："
            f"\n{base_url}/settle"
            "\n（結算完才會開下一期的額度，帳還是照記，不用擔心。）")


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
    return "\n".join(out)

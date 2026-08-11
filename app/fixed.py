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
    {"name": "房租（Zelle 給媽媽）", "amount": 1000.0, "cadence": "monthly",
     "cat": "rent", "where": "Chase"},
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
    return got if isinstance(got, list) and got else [dict(r) for r in fallback]


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
                      ("name", "amount", "cadence", "cat", "where", "next_due", "note")
                      if r.get(k) is not None} | {"amount": amt})
    await set_kv(session, KEY, json.dumps(clean, ensure_ascii=False))


async def monthly_total(session, include_sinking: bool | None = None) -> float:
    return round(sum(r["monthly"] for r in await rows(session, include_sinking)), 2)


async def per_period(session, key: str, include_sinking: bool | None = None) -> float:
    """Charged to one half-month, weighted by real day count (15/31 vs 16/31)."""
    return round(P.split_monthly(await monthly_total(session, include_sinking), key), 2)


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
    """Every non-monthly hit in the next N months, so a dry spell can be seen coming."""
    today = now().date()
    horizon = date(today.year + (today.month + months - 1) // 12,
                   (today.month + months - 1) % 12 + 1, 1)
    out = []
    for r in await rows(session, include_sinking=True):
        cad = r.get("cadence") or "monthly"
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

"""User financial preferences (fixed costs, savings goal, income profile), stored in the KV table."""
from __future__ import annotations

import json
import re

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
                clean.append({
                    "amount": amt,
                    "when": u.get("when"),
                    "note": u.get("note"),
                    "status": u.get("status") or "pending",
                })
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


async def add_invoice(session, amount, when=None, note=None) -> dict:
    """Record a new expected payment Momo just booked."""
    items = _load_list(await get_kv(session, "cfg_upcoming"))
    item = {"amount": float(amount), "when": when, "note": note, "status": "pending"}
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

"""Net worth = every account counted once, from two sources at the same time.

  • Bank-synced accounts (SimpleFIN → Chase checking/saving/Freedom card…) are read LIVE — no
    manual entry needed. Credit cards there come through as debt.
  • The manual ledger (starter pack / chat) covers what the bank can't reach — Apple Card, Apple
    Savings, Venmo, cash.

A manual account that clearly duplicates a synced one is dropped (synced wins), so nothing is
double-counted. With no accounts at all, net worth is just whatever the bank returns."""
from __future__ import annotations

import re

from sqlalchemy import select

from . import prefs
from .models import Account

_CARD_WORDS = ("freedom", "credit", "card", "visa", "mastercard", "amex", "discover")


def norm(name: str) -> str:
    return re.sub(r"[^a-z0-9一-鿿]+", "", (name or "").lower())


def _amt(a) -> float:
    try:
        return float(a.get("amount") or 0)
    except (TypeError, ValueError):
        return 0.0


def _dupes(manual_name: str, synced_norms: list[str]) -> bool:
    """True if this manual account looks like one the bank already syncs (same/contained name)."""
    m = norm(manual_name)
    if len(m) < 4:
        return False
    for s in synced_norms:
        if len(s) >= 4 and (m == s or m in s or s in m):
            return True
    return False


async def compute(session) -> dict:
    synced = (await session.execute(select(Account))).scalars().all()
    prof = await prefs.get_income_profile(session)
    ledger = [a for a in (prof.get("accounts") or []) if _amt(a) > 0]

    assets, debts, rows = 0.0, 0.0, []
    synced_norms = [norm(a.name) for a in synced]

    for a in synced:
        bal = a.balance or 0.0
        is_card = any(w in norm(a.name) for w in _CARD_WORDS)
        if bal < 0 or is_card:
            owed = abs(bal)
            debts += owed
            rows.append({"name": a.name, "kind": "欠款", "amount": -owed, "src": "同步"})
        else:
            assets += bal
            rows.append({"name": a.name, "kind": "現金", "amount": bal, "src": "同步"})

    for a in ledger:
        if _dupes(a.get("name"), synced_norms):
            continue  # the bank already covers this account
        amt = _amt(a)
        if a.get("type") == "credit":
            debts += amt
            rows.append({"name": a.get("name") or "帳戶", "kind": "欠款", "amount": -amt, "src": "手動"})
        else:
            assets += amt
            rows.append({"name": a.get("name") or "帳戶", "kind": "現金", "amount": amt, "src": "手動"})

    if synced and ledger:
        source = "hybrid"
    elif synced:
        source = "synced"
    else:
        source = "ledger"

    return {
        "assets": round(assets, 2),
        "debts": round(debts, 2),
        "net": round(assets - debts, 2),
        "rows": rows,
        "source": source,
    }

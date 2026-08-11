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

# Same account wearing two names: the manual-ledger name (left keywords) is the SAME account as
# a bank-synced one (right keywords) — e.g. Momo's J.P. Morgan investment IS Chase "Self-Directed
# (7435)". If any left-keyword hits the manual name and any right-keyword hits a synced name,
# the manual copy is dropped (synced wins).
_ALIASES = [
    (("jpmorgan", "jpmorganinvestment", "investment"), ("selfdirected",)),
]


def norm(name: str) -> str:
    return re.sub(r"[^a-z0-9一-鿿]+", "", (name or "").lower())


def _amt(a) -> float:
    try:
        return float(a.get("amount") or 0)
    except (TypeError, ValueError):
        return 0.0


def _dupes(manual_name: str, synced_norms: list[str]) -> bool:
    """True if this manual account looks like one the bank already syncs (same/contained name,
    or a known alias like J.P. Morgan ↔ Self-Directed)."""
    m = norm(manual_name)
    if len(m) < 4:
        return False
    for s in synced_norms:
        if len(s) >= 4 and (m == s or m in s or s in m):
            return True
    for left, right in _ALIASES:
        if any(k in m for k in left) and any(any(k in s for k in right) for s in synced_norms):
            return True
    return False


async def compute(session) -> dict:
    synced = (await session.execute(select(Account))).scalars().all()
    prof = await prefs.get_income_profile(session)
    # credit cards stay listed even at $0 owed; cash accounts need a balance to show
    ledger = [a for a in (prof.get("accounts") or [])
              if _amt(a) > 0 or (a.get("type") == "credit" and a.get("name"))]

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

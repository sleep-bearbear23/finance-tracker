"""Net worth = every account counted once, including the ones the bank sync can't see.

Momo's starter-pack ledger is the source of truth when it exists: it lists ALL accounts with
explicit cash/credit types (Chase, Apple Card, Apple Cash, etc.), so Apple — which she has to
track by hand — is always in the total. The live SimpleFIN balances are shown only as reference
so she never double-counts. With no ledger yet, she falls back to the synced balances alone."""
from __future__ import annotations

import re

from sqlalchemy import select

from . import prefs
from .models import Account


def norm(name: str) -> str:
    return re.sub(r"[^a-z0-9一-鿿]+", "", (name or "").lower())


def _amt(a) -> float:
    try:
        return float(a.get("amount") or 0)
    except (TypeError, ValueError):
        return 0.0


async def compute(session) -> dict:
    prof = await prefs.get_income_profile(session)
    ledger = [a for a in (prof.get("accounts") or []) if _amt(a) > 0]
    assets, debts, rows = 0.0, 0.0, []

    if ledger:
        for a in ledger:
            amt = _amt(a)
            nm = a.get("name") or "帳戶"
            if a.get("type") == "credit":
                debts += amt
                rows.append({"name": nm, "kind": "欠款", "amount": -amt})
            else:
                assets += amt
                rows.append({"name": nm, "kind": "現金", "amount": amt})
        source = "ledger"
    else:
        accts = (await session.execute(select(Account))).scalars().all()
        for a in accts:
            bal = a.balance or 0.0
            if bal < 0:
                debts += -bal
                rows.append({"name": a.name, "kind": "欠款", "amount": bal})
            else:
                assets += bal
                rows.append({"name": a.name, "kind": "現金", "amount": bal})
        source = "synced"

    return {
        "assets": round(assets, 2),
        "debts": round(debts, 2),
        "net": round(assets - debts, 2),
        "rows": rows,
        "source": source,
    }

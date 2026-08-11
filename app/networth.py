"""Net worth — computed from the one account registry, so it can never disagree with the
account list Momo sees on the dashboard.

The registry already resolves the hard parts: Chase comes from the bank sync, Apple Card /
Apple Savings / Venmo come from what Momo reports, duplicates (a manual entry for an account
the bank already syncs, or an alias like J.P. Morgan ↔ Self-Directed) are dropped. Here we
just add it up: cash − what's owed.
"""
from __future__ import annotations

from . import accounts as acct

# kept for callers that still import these
norm = acct.norm


async def compute(session) -> dict:
    reg = await acct.registry(session)
    real = [a for a in reg.values() if a["kind"] in ("cash", "credit")]

    assets = debts = 0.0
    rows = []
    for a in sorted(real, key=lambda a: (a["kind"] != "cash", -(a["balance"] or 0))):
        bal = a["balance"] or 0.0
        src = "同步" if a["balance_src"] == "同步" else "手動"
        if a["kind"] == "credit":
            debts += bal
            rows.append({"name": a["name"], "kind": "欠款", "amount": -bal, "src": src})
        else:
            assets += bal
            rows.append({"name": a["name"], "kind": "現金", "amount": bal, "src": src})

    has_sync = any(a["balance_src"] == "同步" for a in real)
    has_manual = any(a["balance_src"] != "同步" for a in real)
    source = "hybrid" if (has_sync and has_manual) else ("synced" if has_sync else "ledger")

    return {
        "assets": round(assets, 2),
        "debts": round(debts, 2),
        "net": round(assets - debts, 2),
        "rows": rows,
        "source": source,
    }

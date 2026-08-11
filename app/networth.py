"""Net worth — computed from the one account registry, so it can never disagree with the
account list Momo sees on the dashboard.

The registry already resolves the hard parts: Chase comes from the bank sync, Apple Card /
Apple Savings / Venmo come from what Momo reports, duplicates (a manual entry for an account
the bank already syncs, or an alias like J.P. Morgan ↔ Self-Directed) are dropped. Here we
just add it up — and, importantly, keep three different questions apart:

  net        what she's worth              — everything, at face value
  spendable  what she could spend today    — cash only, no brokerage, no credit
  runway     what could carry a dry spell  — cash plus the brokerage at a haircut

Conflating those is how a stock account turns into grocery money on a spreadsheet.
"""
from __future__ import annotations

from . import accounts as acct

# kept for callers that still import these
norm = acct.norm

_KIND_LABEL = {"cash": "現金", "credit": "欠款", "invest": "投資"}


async def compute(session) -> dict:
    reg = await acct.registry(session)
    real = [a for a in reg.values() if a["kind"] in ("cash", "credit", "invest")]

    assets = debts = spendable = runway = invest = 0.0
    rows = []
    order = {"cash": 0, "invest": 1, "credit": 2}
    for a in sorted(real, key=lambda a: (order[a["kind"]], -(a["balance"] or 0))):
        bal = a["balance"] or 0.0
        src = "同步" if a["balance_src"] == "同步" else "手動"
        if a["kind"] == "credit":
            debts += bal
            amount = -bal
        else:
            assets += bal
            amount = bal
            runway += a.get("runway_value", bal)
            if a["kind"] == "cash":
                spendable += bal
            else:
                invest += bal
        rows.append({
            "name": a["name"], "kind": _KIND_LABEL[a["kind"]], "amount": amount,
            "src": src, "liquid": a.get("liquid", a["kind"] == "cash"),
            "volatile": a.get("volatile", False),
        })

    has_sync = any(a["balance_src"] == "同步" for a in real)
    has_manual = any(a["balance_src"] != "同步" for a in real)
    source = "hybrid" if (has_sync and has_manual) else ("synced" if has_sync else "ledger")

    return {
        "assets": round(assets, 2),
        "debts": round(debts, 2),
        "net": round(assets - debts, 2),
        # money she could actually spend this afternoon — brokerage deliberately excluded
        "spendable": round(spendable, 2),
        "invest": round(invest, 2),
        # cash + brokerage at a haircut, minus what's owed: what a dry spell can lean on
        "runway_assets": round(runway, 2),
        "runway_net": round(runway - debts, 2),
        "haircut": acct.INVEST_HAIRCUT,
        "rows": rows,
        "source": source,
    }

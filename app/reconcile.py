"""Apple Card month-end reconciliation.

Charges captured live (iOS tap → 'shortcut', or told over LINE → 'manual') are matched
against the authoritative SimpleFIN statement when it finally arrives. The bank record is
kept as truth; the live duplicate is marked 'reconciled' so it drops out of every total.
"""
from __future__ import annotations

from sqlalchemy import select

from .config import aware
from .models import Transaction

AMOUNT_TOL = 0.01
DAY_TOL = 4  # a tap and its statement line can be a few days apart


def _matches(bank, live, day_tol: int = DAY_TOL) -> bool:
    if abs(abs(bank.amount) - abs(live.amount)) > AMOUNT_TOL:
        return False
    db = aware(bank.posted_at or bank.created_at)
    dl = aware(live.posted_at or live.created_at)
    if not db or not dl:
        return False
    return abs((db - dl).days) <= day_tol


async def reconcile(session) -> int:
    rows = (await session.execute(select(Transaction).where(Transaction.amount < 0))).scalars().all()
    bank = [t for t in rows if t.source == "simplefin"]
    live = [t for t in rows if t.source in ("shortcut", "manual") and t.status != "reconciled"]

    merged = 0
    for b in bank:
        for l in live:
            if l.status == "reconciled":
                continue
            if _matches(b, l):
                # carry the human context onto the authoritative bank record
                if l.note and not b.note:
                    b.note = l.note
                if l.category and not b.category:
                    b.category = l.category
                if b.status in ("needs_context", "prompted", "auto"):
                    b.status = "enriched"
                l.status = "reconciled"  # drop the live duplicate from all totals
                merged += 1
                break
    await session.commit()
    return merged

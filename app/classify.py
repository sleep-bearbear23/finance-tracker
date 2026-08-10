"""Shared transaction classification — transfer vs income vs spend, using merchant memory.
Used by both the SimpleFIN poller and the daily screenshot reader so they behave identically."""
from __future__ import annotations

from . import categories
from .models import MerchantMemory


async def classify(session, desc: str, amount: float, backfill: bool = False):
    """Return (status, category, note).
    backfill=True keeps history silent; otherwise unknown items become 'needs_context' (she asks)."""
    if categories.is_transfer(desc):
        return "ignored", "Transfers/Ignore", None

    mem = await session.get(MerchantMemory, categories.merchant_key(desc))
    if mem is not None:
        if mem.is_income is True:
            return "income", "Income", mem.note
        if mem.is_income is False:
            return "ignored", "Transfers/Ignore", mem.note
        return "auto", (mem.category or categories.guess(desc)), mem.note

    if amount > 0:  # money in — real income or a payback?
        return ("income", "Income", None) if backfill else ("needs_context", None, None)
    return ("auto", categories.guess(desc), None) if backfill else ("needs_context", categories.guess(desc), None)

"""Shared transaction classification — transfer vs income vs spend, using merchant memory.
Used by both the SimpleFIN poller and the daily screenshot reader so they behave identically."""
from __future__ import annotations

from . import categories, prefs
from .models import MerchantMemory


async def classify(session, desc: str, amount: float, backfill: bool = False):
    """Return (status, category, note).
    backfill=True keeps history silent; otherwise unknown items become 'needs_context' (she asks)."""
    if categories.is_transfer(desc):
        return "ignored", categories.TRANSFER, None

    mem = await session.get(MerchantMemory, categories.merchant_key(desc))
    if mem is not None:
        if mem.is_income is True:
            return "income", "Income", mem.note
        if mem.is_income is False:
            return "ignored", categories.TRANSFER, mem.note
        return "auto", (mem.category or categories.guess(desc)), mem.note

    if amount > 0:  # money in — work pay, or a payback / bill-split?
        # Only treat it as income if it's from a known work payer. Everything else is NOT
        # assumed to be pay: silent history files it as a transfer, live deposits get asked.
        if await prefs.is_work_income_source(session, desc):
            return "income", "Income", None
        return ("ignored", categories.TRANSFER, None) if backfill else ("needs_context", None, None)
    return ("auto", categories.guess(desc), None) if backfill else ("needs_context", categories.guess(desc), None)

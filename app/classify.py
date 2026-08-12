"""Shared transaction classification — transfer vs income vs spend, using merchant memory.
Used by both the SimpleFIN poller and the daily screenshot reader so they behave identically."""
from __future__ import annotations

from . import categories, prefs
from . import taxonomy as T
from .models import MerchantMemory

#: Where a family payback lands. It cannot be matched to the specific purchase it
#: reverses — Momo buys household things across a fortnight and her mother settles up
#: in one transfer — so it goes to the household bucket, which is 彈性. That is enough:
#: the arithmetic nets whole buckets, not individual pairs.
FAMILY_PAYBACK_CATEGORY = "household"


async def classify(session, desc: str, amount: float, backfill: bool = False):
    """Return (status, category, note, inflow_kind).

    backfill=True keeps history silent; otherwise unknown items become 'needs_context'
    (she asks)."""
    # Ahead of the transfer check on purpose. 「Online Transfer From CHK ...7567」 matches
    # 「online transfer」, so without this it is filed as Momo shuffling her own money and
    # the offset to a purchase she has already been charged for disappears.
    if T.family_payback(desc, amount):
        return "auto", FAMILY_PAYBACK_CATEGORY, "媽媽回款", T.REIMBURSE_FAMILY

    if categories.is_transfer(desc):
        return "ignored", categories.TRANSFER, None, None

    mem = await session.get(MerchantMemory, categories.merchant_key(desc))
    if mem is not None:
        if mem.is_income is True:
            return "income", "Income", mem.note, T.PAY
        if mem.is_income is False:
            return "ignored", categories.TRANSFER, mem.note, None
        return "auto", (mem.category or categories.guess(desc)), mem.note, None

    if amount > 0:  # money in — work pay, or a payback / bill-split?
        # Only treat it as income if it's from a known work payer. Everything else is NOT
        # assumed to be pay: silent history files it as a transfer, live deposits get asked.
        if await prefs.is_work_income_source(session, desc):
            return "income", "Income", None, T.PAY
        return (("ignored", categories.TRANSFER, None, None) if backfill
                else ("needs_context", None, None, None))
    return (("auto", categories.guess(desc), None, None) if backfill
            else ("needs_context", categories.guess(desc), None, None))

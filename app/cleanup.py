"""One-shot data hygiene passes, run at boot behind KV flags.

v1: with the much wider rule set (ported from the Apple Card statement analysis),
re-guess every still-uncategorized transaction, and sweep card-payment/transfer
rows that slipped past the old keyword list out of "spending"."""
from __future__ import annotations

from sqlalchemy import select

from . import categories
from .db import get_kv, set_kv
from .models import Transaction


async def run(session) -> tuple[int, int]:
    """Returns (n_categorized, n_swept_transfers)."""
    if await get_kv(session, "cleanup_v1") == "1":
        return 0, 0

    rows = (await session.execute(select(Transaction))).scalars().all()
    n_cat = n_xfer = 0
    for t in rows:
        # Card payments / internal moves misfiled as spending → ignore. Never touch
        # rows Momo already labeled with a real category.
        if (t.category in (None, "Transfers/Ignore")
                and t.status not in ("ignored", "reconciled")
                and categories.is_transfer(t.merchant_desc)):
            t.status = "ignored"
            t.category = "Transfers/Ignore"
            n_xfer += 1
            continue
        # Fill empty categories with the new, wider guess. Existing labels stay.
        if t.category is None and t.status not in ("ignored", "reconciled"):
            g = categories.guess(t.merchant_desc)
            if g:
                t.category = g
                n_cat += 1

    await set_kv(session, "cleanup_v1", "1")
    if n_cat or n_xfer:
        await session.commit()
    return n_cat, n_xfer

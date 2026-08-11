"""One-shot data hygiene passes, run at boot behind KV flags.

v1: with the much wider rule set (ported from the Apple Card statement analysis),
re-guess every still-uncategorized transaction, and sweep card-payment/transfer
rows that slipped past the old keyword list out of "spending"."""
from __future__ import annotations

from sqlalchemy import select

from . import categories, prefs
from .db import get_kv, set_kv
from .models import Transaction

# The manual-ledger accounts Momo actually has (the ones no bank sync can reach).
# Guaranteed to exist so the dash always lists them — balances start at 0 until
# Momo states them ("Apple Card 現在欠 600") or re-sends the starter pack.
_KNOWN_MANUAL_ACCOUNTS = [
    {"name": "Apple Card", "type": "credit"},
    {"name": "Apple Goldman Sachs Savings", "type": "cash"},
    {"name": "Venmo", "type": "cash"},
]


async def dedupe_ledger(session) -> int:
    """Drop manual ledger entries that duplicate an account the bank already syncs
    (or a same-account alias). Runs once; the bank copy always wins."""
    from sqlalchemy import select as _select

    from . import accounts as acct
    from .models import Account

    if await get_kv(session, "dedupe_ledger_v1") == "1":
        return 0
    synced = [acct.norm(a.name) for a in
              (await session.execute(_select(Account))).scalars().all()]
    prof = await prefs.get_income_profile(session)
    ledger = prof.get("accounts") or []
    kept, dropped = [], 0
    seen: list[str] = []
    for m in ledger:
        nm = m.get("name") or ""
        if acct.norm(nm) and (acct.duplicates(nm, synced) or acct.duplicates(nm, seen)):
            dropped += 1
            continue
        seen.append(acct.norm(nm))
        kept.append(m)
    if dropped:
        await prefs.set_income_profile(session, {"accounts": kept})
    await set_kv(session, "dedupe_ledger_v1", "1")
    return dropped


async def ensure_accounts(session) -> int:
    """Make sure Momo's known manual accounts exist in the ledger. Returns how many were added."""
    if await get_kv(session, "ensure_accounts_v1") == "1":
        return 0
    prof = await prefs.get_income_profile(session)
    have = {prefs._norm(a.get("name")) for a in prof.get("accounts", [])}
    added = 0
    for a in _KNOWN_MANUAL_ACCOUNTS:
        if prefs._norm(a["name"]) not in have:
            await prefs.update_account(session, a["name"], 0.0, a["type"])
            added += 1
    await set_kv(session, "ensure_accounts_v1", "1")
    return added


async def run(session) -> tuple[int, int]:
    """Returns (n_categorized, n_swept_transfers). Flag bumps whenever the rules widen,
    so each rules upgrade re-sweeps history once."""
    if await get_kv(session, "cleanup_v2") == "1":
        return 0, 0

    rows = (await session.execute(select(Transaction))).scalars().all()
    n_cat = n_xfer = 0
    for t in rows:
        # Card payments / internal moves misfiled as spending → ignore. Never touch
        # rows Momo already labeled with a real category.
        if (t.category in (None, categories.TRANSFER)
                and t.status not in ("ignored", "reconciled")
                and categories.is_transfer(t.merchant_desc)):
            t.status = "ignored"
            t.category = categories.TRANSFER
            n_xfer += 1
            continue
        # Fill empty categories with the new, wider guess. Existing labels stay.
        if t.category is None and t.status not in ("ignored", "reconciled"):
            g = categories.guess(t.merchant_desc)
            if g:
                t.category = g
                n_cat += 1

    await set_kv(session, "cleanup_v2", "1")
    if n_cat or n_xfer:
        await session.commit()
    return n_cat, n_xfer

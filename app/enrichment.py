"""The enrichment loop: group new charges after a quiet window, then reconcile replies."""
from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

from sqlalchemy import select

from . import categories, line_client, llm, memory
from .config import aware, now, settings
from .db import get_kv
from .models import MerchantMemory, Transaction

OWNER_KEY = "owner_user_id"


async def flush_pending(session) -> None:
    """If new charges have been quiet for DEBOUNCE_MINUTES, ask about them in one grouped message."""
    owner = await get_kv(session, OWNER_KEY)
    if not owner:
        return  # nobody to text yet — wait until Momo messages her once

    rows = (await session.execute(
        select(Transaction)
        .where(Transaction.status == "needs_context", Transaction.prompted_at.is_(None))
        .order_by(Transaction.created_at, Transaction.id)  # fully deterministic order
    )).scalars().all()
    if not rows:
        return

    newest = max(aware(r.created_at) for r in rows)
    if now() - newest < timedelta(minutes=settings.DEBOUNCE_MINUTES):
        return  # still inside the quiet window; let the burst settle

    batch_id = str(uuid4())
    stamp = now()
    # Freeze the exact 1..n order the user will see, so their reply reconciles correctly.
    for seq, r in enumerate(rows, 1):
        r.prompted_at = stamp
        r.batch_id = batch_id
        r.batch_seq = seq
        r.status = "prompted"
    await session.commit()

    text = await llm.enrichment_prompt(rows)
    await line_client.push(owner, text)
    await memory.remember(session, "assistant", text)


async def handle_reply(session, reply_text: str) -> tuple[bool, str | None]:
    """Try to interpret an incoming message as an answer to the pending charges."""
    rows = (await session.execute(
        select(Transaction)
        .where(Transaction.status == "prompted")
        .order_by(Transaction.prompted_at.desc())
    )).scalars().all()
    if not rows:
        return False, None

    latest = rows[0].batch_id
    batch = [r for r in rows if r.batch_id == latest]
    batch.sort(key=lambda r: r.batch_seq or 0)  # exact order the user saw

    mapping = await llm.parse_reply(batch, reply_text)
    for r in batch:
        m = mapping.get(r.batch_seq)
        if m:
            if m.get("note"):
                r.note = m["note"]
            if m.get("category"):
                r.category = m["category"]
            is_income = m.get("is_income")
        else:
            is_income = None

        # Set the transaction's fate, and remember this merchant/sender so she never re-asks.
        if r.amount > 0:  # money in — was it real income or just a payback/transfer?
            if is_income is True:
                r.status, r.category = "income", "Income"
            elif is_income is False:
                r.status, r.category = "ignored", "Transfers/Ignore"
            else:
                r.status = "enriched"
        else:
            r.status = "enriched"

        await _remember(session, r, is_income)

    await session.commit()
    confirm = await llm.enrichment_confirm(batch)
    return True, confirm


async def _remember(session, txn, is_income) -> None:
    """Save what we just learned about this merchant/sender for next time."""
    key = categories.merchant_key(txn.merchant_desc)
    mem = await session.get(MerchantMemory, key)
    if mem is None:
        mem = MerchantMemory(key=key)
        session.add(mem)
    mem.category = txn.category
    mem.note = txn.note
    mem.is_income = is_income
    mem.necessary = (txn.category in categories.FIXED_HINT)
    mem.updated_at = now()

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


async def pending_batch(session) -> list:
    """The numbered charges she is currently waiting on an answer for."""
    rows = (await session.execute(
        select(Transaction)
        .where(Transaction.status == "prompted")
        .order_by(Transaction.prompted_at.desc())
    )).scalars().all()
    if not rows:
        return []
    latest = rows[0].batch_id
    batch = [r for r in rows if r.batch_id == latest]
    batch.sort(key=lambda r: r.batch_seq or 0)  # exact order the user saw
    return batch


async def file_reply(session, reply_text: str) -> bool:
    """Interpret a message as an answer to the pending charges and file them.

    Returns False when the reply maps to nothing — this used to mark the whole batch
    'enriched' regardless, so one off-topic message could silently close every open
    charge with no category on it."""
    batch = await pending_batch(session)
    if not batch:
        return False

    mapping = await llm.parse_reply(batch, reply_text)
    if not mapping:
        return False
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
                r.status, r.category = "ignored", categories.TRANSFER
            else:
                r.status = "enriched"
        else:
            r.status = "enriched"

        await _remember(session, r, is_income)

    await session.commit()
    return True


async def apply_reply(session, reply_text: str) -> dict:
    """Same work, reported as data — for the tool the agent calls.

    She writes her own confirmation afterwards from the tool result, so nothing is said
    about a charge that did not actually get filed."""
    batch = await pending_batch(session)
    if not batch:
        return {"ok": False, "error": "現在沒有等著交代的帳"}
    before = {r.id: {"category": r.category, "note": r.note, "status": r.status,
                     "inflow_kind": r.inflow_kind} for r in batch}
    if not await file_reply(session, reply_text):
        return {"ok": False, "error": "這句話對不上那幾筆帳，沒有動它們",
                "items": [{"n": r.batch_seq, "merchant": r.merchant_desc,
                           "amount": round(r.amount, 2)} for r in batch]}
    done = [{"n": r.batch_seq, "merchant": r.merchant_desc, "amount": round(r.amount, 2),
             "category": r.category, "note": r.note, "status": r.status} for r in batch]
    return {"ok": True, "before": before, "items": done, "n": len(done)}


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

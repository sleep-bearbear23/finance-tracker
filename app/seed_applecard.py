"""One-time Apple Card history import: 19 statements (2025-01 → 2026-07), 1,727 transactions,
parsed from Momo's statement PDFs and pre-categorized by brand rules (94%).

Idempotent: rows key on a stable id; a KV flag skips the whole pass once done. Skips anything
that already exists from a screenshot/manual entry on the same day with the same amount and a
matching merchant, so July screenshots don't double-count.

When Momo finishes the labeling session, app/data/applecard_labels.json appears and
apply_labels() folds the answers in: fixes categories on history AND writes MerchantMemory so
every future charge from those merchants auto-labels.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from sqlalchemy import select

from . import categories
from .config import TZ, aware
from .db import get_kv, set_kv
from .models import MerchantMemory, Transaction

_DATA = Path(__file__).parent / "data" / "applecard.json"
_LABELS = Path(__file__).parent / "data" / "applecard_labels.json"


def _noon(day: str) -> datetime:
    return datetime.strptime(day, "%Y-%m-%d").replace(hour=12, tzinfo=TZ)


async def backfill(session) -> int:
    """Import the statement history once. Returns rows added."""
    if await get_kv(session, "applecard_backfill_v1") == "1" or not _DATA.exists():
        return 0
    rows = json.loads(_DATA.read_text())

    # Existing non-simplefin rows indexed by (date, cents) for cheap dedup against screenshots.
    existing = (await session.execute(
        select(Transaction).where(Transaction.source.in_(("screenshot", "shortcut", "manual")))
    )).scalars().all()
    by_daycents: dict[tuple, list] = {}
    for t in existing:
        d = aware(t.posted_at or t.created_at)
        if d:
            by_daycents.setdefault((d.date().isoformat(), round(abs(t.amount) * 100)), []).append(t)

    added = 0
    for r in rows:
        if await session.get(Transaction, r["id"]):
            continue
        dupes = by_daycents.get((r["date"], round(abs(r["amount"]) * 100)), [])
        key = categories.merchant_key(r["merchant"])
        skip = False
        for t in dupes:
            kb = categories.merchant_key(t.merchant_desc)
            if key == kb or (len(key) >= 4 and len(kb) >= 4 and (key.startswith(kb) or kb.startswith(key))):
                skip = True  # already logged via screenshot/tap — keep the original
                break
        if skip:
            continue
        session.add(Transaction(
            id=r["id"], account_id="applecard", amount=r["amount"],
            merchant_desc=r["merchant"], posted_at=_noon(r["date"]),
            category=r.get("cat"), note=r.get("gkey"),  # gkey kept in note so labels can find it
            status="auto", source="applecard",
        ))
        added += 1

    await set_kv(session, "applecard_backfill_v1", "1")
    if added:
        await session.commit()
    return added


async def apply_labels(session) -> int:
    """Fold Momo's labeling-session answers into history + merchant memory. Idempotent."""
    if not _LABELS.exists():
        return 0
    data = json.loads(_LABELS.read_text())
    ver = str(data.get("version", 1))
    if await get_kv(session, "applecard_labels_v") == ver:
        return 0

    fixes: dict[str, dict] = {}
    for key, a in (data.get("answers") or {}).items():
        if a.get("cat"):
            fixes[key] = a
    for key, cat in (data.get("edits") or {}).items():
        fixes.setdefault(key, {})["cat"] = cat

    touched = 0
    seen_keys: set[str] = set()
    rows = (await session.execute(
        select(Transaction).where(Transaction.source == "applecard")
    )).scalars().all()
    for t in rows:
        gk = t.note if (t.note or "").startswith(("b:", "m:")) else None
        fix = fixes.get(gk) if gk else None
        if not fix or not fix.get("cat"):
            continue
        t.category = fix["cat"]
        if fix.get("note"):
            t.note = fix["note"]
        touched += 1
        # Teach memory ONLY from what Momo explicitly labeled, so her word sticks forever.
        for mk in {categories.merchant_key(t.merchant_desc)}:
            if mk in seen_keys:
                continue
            seen_keys.add(mk)
            mem = await session.get(MerchantMemory, mk)
            if mem is None:
                session.add(MerchantMemory(
                    key=mk, category=fix["cat"], note=fix.get("note"),
                    is_income=None, necessary=bool(fix.get("nec")),
                ))
            else:
                mem.category = fix["cat"]
                if fix.get("note"):
                    mem.note = fix["note"]

    await set_kv(session, "applecard_labels_v", ver)
    await session.commit()
    return touched

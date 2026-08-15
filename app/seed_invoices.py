"""Momo's invoice archive, read once and kept as the day-rate's evidence.

Her day rate was running on n=1 — a single booking whose note happened to say 「拍八天」.
Every other job's day count was sitting in a PDF in her invoice folder, which the running
app cannot see. Momo: "can't you just collect all the data and push it into the system?
Like how we did the apple card statements." Yes.

Thirteen invoices, 2026-01 → 2026-08. Four were text PDFs; the other nine had been flattened
to a single 192-dpi image each, so they were rasterised and read. Two numbers per invoice
matter and they are not the same number:

  rate       what she contracted per shoot day — $200 to $350, rising
  day_total  rate × days, BEFORE prep/wrap fees and overtime

Prep fees and overtime are real income but they are not per-day, so folding them into the
divisor inflates the rate: 「Woman in Blue Dress」 is $300/day contracted and $420/day if you
divide its total by five. The goal asks "how many days must I book", so it wants the
contracted rate, and the extras are carried separately rather than smeared across the days.

This writes to KV only — no Transaction rows. The money is already in the ledger via the
bank feed and her pending list; importing it again would double-count every job.
"""
from __future__ import annotations

import json
from pathlib import Path

from .db import get_kv, set_kv

_DATA = Path(__file__).parent / "data" / "invoices.json"
KEY = "cfg_invoices"
FLAG = "invoice_seed_v1"


def load() -> list[dict]:
    if not _DATA.exists():
        return []
    try:
        rows = json.loads(_DATA.read_text())
    except ValueError:
        return []
    return [r for r in rows if isinstance(r, dict)]


async def invoices(session, overlaid: bool = True) -> list[dict]:
    """The archive, with Momo's own corrections folded in.

    ``kind`` in particular has to come through here: she marks a job as 作品集／無酬 in the
    project overlay, and if the day rate keeps reading the raw archive it goes on averaging
    a $0 day into her price — which is the exact thing she asked to be protected from."""
    raw = await get_kv(session, KEY)
    try:
        rows = json.loads(raw) if raw else []
    except (TypeError, ValueError):
        rows = []
    rows = [r for r in rows if isinstance(r, dict)]
    if not overlaid:
        return rows
    from . import projects as PJ
    over = await PJ.overlay(session)
    out = []
    for r in rows:
        pid = PJ.slug(r.get("project") or r.get("num") or "")
        extra = over.get(pid) or {}
        out.append({**r, **{k: v for k, v in extra.items()
                            if k in ("kind", "rate", "days", "client") and v is not None}})
    return out


async def backfill(session, force: bool = False) -> dict:
    """Store the archive, and attach day counts to the jobs still waiting to be paid.

    Re-runnable with force= when the file grows: an invoice is keyed by its number, so a
    second pass replaces rather than duplicates.
    """
    if not force and await get_kv(session, FLAG) == "1":
        return {}
    rows = load()
    if not rows:
        return {}

    have = {r.get("num"): r for r in await invoices(session)}
    have.update({r.get("num"): r for r in rows})
    merged = sorted(have.values(), key=lambda r: r.get("date") or "")
    await set_kv(session, KEY, json.dumps(merged, ensure_ascii=False))

    # Where an invoice names a job she is still owed for, give that job its day count so
    # 已排定 knows how many shoot days are already committed.
    from . import prefs
    attached = 0
    profile = await prefs.get_income_profile(session)
    items = profile["upcoming"]
    changed = False
    for inv in rows:
        want, days = inv.get("match"), inv.get("days")
        if not want or not days:
            continue
        for it in items:
            note = (it.get("note") or "")
            if want.lower() in note.lower() and not it.get("days"):
                it["days"] = int(days)
                attached += 1
                changed = True
                break
    if changed:
        await set_kv(session, "cfg_upcoming", json.dumps(items))

    await set_kv(session, FLAG, "1")
    with_days = [r for r in merged if r.get("days")]
    return {"invoices": len(merged), "with_days": len(with_days),
            "days": sum(int(r["days"]) for r in with_days),
            "attached": attached}

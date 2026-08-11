"""Record every write, and be able to take it back.

Momo asked for 陳會計 to manage the data herself, and agreed to give her full authority
on the condition that every write reports what it changed. That condition is this module.

The trick that makes undo cheap: almost everything mutable in this app lives in the KV
table as a JSON blob (`cfg_upcoming`, `cfg_accounts`, `cfg_fixed_costs`, the scalars).
So a tool does not have to describe its own inverse — we photograph the keys it might
touch before it runs, photograph them after, and store the difference. Restoring is
writing the "before" side back.

Rows in real tables (a manually logged charge, a merchant memory) are handled the same
way, one dict of columns each, with ``before=None`` meaning "this did not exist" — so
undoing a creation is a delete.
"""
from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import datetime

from sqlalchemy import delete, select

from .config import now
from .db import get_kv, set_kv
from .models import Change, KV, MerchantMemory, Transaction

#: The keys any tool is allowed to move. Photographed before and after every call — if a
#: tool touches something not on this list, the change is invisible to the log, so add it
#: here in the same commit that adds the tool.
WATCHED: tuple[str, ...] = (
    "cfg_upcoming", "cfg_accounts", "cfg_fixed_costs", "cfg_sinking", "cfg_sinking_on",
    "cfg_savings_amount", "cfg_savings_cadence", "cfg_emergency_target",
    "cfg_monthly_baseline", "cfg_fixed_monthly", "cfg_budget_start",
    "cfg_cash_on_hand", "cfg_total_debt", "cfg_income_sources", "cfg_season",
)

_TABLES = {"transactions": Transaction, "merchant_memory": MerchantMemory}


async def _snapshot(session, keys) -> dict[str, str | None]:
    return {k: await get_kv(session, k) for k in keys}


def _diff(before: dict, after: dict) -> dict:
    return {k: {"before": before.get(k), "after": after.get(k)}
            for k in after if before.get(k) != after.get(k)}


class Recorder:
    """Handed to a tool so it can register row-level changes the KV diff cannot see."""

    def __init__(self):
        self.rows: list[dict] = []
        self.summary: str = ""

    def row(self, table: str, ident, before: dict | None, after: dict | None) -> None:
        self.rows.append({"table": table, "id": str(ident), "before": before, "after": after})

    def says(self, summary: str) -> None:
        """The one line Momo will read. Say the before and the after, both."""
        self.summary = summary


@asynccontextmanager
async def watching(session, *, tool: str, args: dict | None = None,
                   source_text: str | None = None, actor: str = "line",
                   keys: tuple[str, ...] = WATCHED):
    """Run a tool inside this and whatever it changed becomes one reversible Change row.

    Yields a :class:`Recorder`. A tool that changes nothing writes no row — "I set it to
    what it already was" is not an event, and a log full of those is a log nobody reads.
    """
    rec = Recorder()
    before = await _snapshot(session, keys)
    try:
        yield rec
    finally:
        after = await _snapshot(session, keys)
        patch = {"kv": _diff(before, after), "rows": rec.rows}
        if patch["kv"] or patch["rows"]:
            session.add(Change(
                at=now(), actor=actor, tool=tool,
                args=json.dumps(args or {}, ensure_ascii=False),
                summary=rec.summary, patch=json.dumps(patch, ensure_ascii=False),
                source_text=source_text))
            await session.commit()


async def recent(session, limit: int = 60, include_undone: bool = True) -> list[dict]:
    q = select(Change).order_by(Change.at.desc(), Change.id.desc()).limit(limit)
    rows = (await session.execute(q)).scalars().all()
    out = []
    for c in rows:
        if not include_undone and c.undone_at:
            continue
        out.append({
            "id": c.id, "at": c.at.isoformat() if c.at else None, "actor": c.actor,
            "tool": c.tool, "summary": c.summary, "source_text": c.source_text,
            "undone_at": c.undone_at.isoformat() if c.undone_at else None,
            "undoable": c.undone_at is None,
        })
    return out


async def undo(session, change_id: int) -> dict:
    """Put back exactly what was there before, and say so. Idempotent by the undone_at flag."""
    c = await session.get(Change, change_id)
    if c is None:
        return {"ok": False, "error": "沒有這筆紀錄"}
    if c.undone_at:
        return {"ok": False, "error": "這筆已經還原過了"}
    try:
        patch = json.loads(c.patch or "{}")
    except ValueError:
        return {"ok": False, "error": "紀錄壞掉了，沒辦法還原"}

    for key, side in (patch.get("kv") or {}).items():
        prior = side.get("before")
        if prior is None:
            await session.execute(delete(KV).where(KV.key == key))
        else:
            await set_kv(session, key, prior)

    for r in reversed(patch.get("rows") or []):
        model = _TABLES.get(r.get("table"))
        if model is None:
            continue
        pk = list(model.__table__.primary_key.columns)[0].name
        ident = r.get("id")
        obj = await session.get(model, ident)
        prior = r.get("before")
        if prior is None:                       # it was created — take it away again
            if obj is not None:
                await session.delete(obj)
            continue
        if obj is None:                          # it was deleted — put it back
            obj = model(**{pk: ident})
            session.add(obj)
        for k, v in prior.items():
            setattr(obj, k, _revive(model, k, v))

    c.undone_at = now()
    await session.commit()
    return {"ok": True, "summary": c.summary}


def _revive(model, column: str, value):
    """JSON gives back strings; the datetime columns want datetimes."""
    col = model.__table__.columns.get(column)
    if value is None or col is None:
        return value
    if str(col.type).startswith("TIMESTAMP") or "DATETIME" in str(col.type).upper():
        try:
            return datetime.fromisoformat(value)
        except (TypeError, ValueError):
            return None
    return value


def snapshot_row(obj, columns: list[str]) -> dict:
    """Photograph the columns of a row, JSON-safe, for the before/after side of a patch."""
    out = {}
    for c in columns:
        v = getattr(obj, c, None)
        out[c] = v.isoformat() if isinstance(v, datetime) else v
    return out

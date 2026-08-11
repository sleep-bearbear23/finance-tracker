"""Proactive overspend nudges — she pings you when the half-month allowance runs hot."""
from __future__ import annotations

from . import budget, line_client, llm, memory
from .db import get_kv, set_kv


async def check(session) -> None:
    owner = await get_kv(session, "owner_user_id")
    if not owner:
        return
    b = await budget.status(session)
    if not b["allowance"] or b["pct_used"] is None:
        return  # no budget configured yet

    key = f"alerted:{b['period_key']}"
    already = await get_kv(session, key, "") or ""

    level = None
    if b["pct_used"] >= 100 and "100" not in already:
        level = "100"
    elif b["pct_used"] >= 80 and "80" not in already:
        level = "80"
    if not level:
        return  # nothing new to nag about this period

    msg = await llm.overspend_nudge(b, level)
    await line_client.push(owner, msg)
    await memory.remember(session, "assistant", msg)
    await set_kv(session, key, (already + "," + level).strip(","))

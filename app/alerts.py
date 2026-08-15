"""Proactive overspend nudges — she pings you when the half-month allowance runs hot."""
from __future__ import annotations

from . import budget, line_client, llm, memory
from .db import get_kv, set_kv


async def check(session) -> None:
    owner = await get_kv(session, "owner_user_id")
    if not owner:
        return
    b = await budget.status(session)
    if not b.get("configured"):
        return  # genuinely not set up — nothing to grade against
    if b["pct_used"] is None:
        # The allowance itself is zero or negative — a lean period, not a config gap.
        # The old guard returned here silently forever, which is how the overspend
        # alerts died without anyone noticing. Lean periods are exactly when a heads-up
        # matters, so say it once per period.
        key0 = f"alerted:{b['period_key']}"
        already0 = await get_kv(session, key0, "") or ""
        if "lean" not in already0:
            msg = (f"這期（{b['period_label']}）額度算出來是 ${b['allowance']:,.0f}——"
                   "不是你花掉的，是進來的不夠付固定開銷。缺口從水位補，先照常過日子，"
                   "細節看儀表板。")
            await line_client.push(owner, msg)
            await memory.remember(session, "assistant", msg)
            await set_kv(session, key0, (already0 + ",lean").strip(","))
        return

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

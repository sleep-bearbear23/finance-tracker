"""First-run interview: she asks for fixed monthly costs + a savings goal, so the
budget math has something to work with. Income basis comes from real deposits, so she
doesn't need to ask about that."""
from __future__ import annotations

from . import line_client, llm, prefs
from .db import get_kv, set_kv


async def is_pending(session) -> bool:
    return (await get_kv(session, "onboarding_done")) != "1"


async def start(session, owner: str) -> None:
    await line_client.push(owner, await llm.onboarding_intro())


async def handle(session, text: str) -> tuple[str, bool]:
    """Parse whatever the user said, store what we got, return (reply, done)."""
    parsed = await llm.parse_onboarding(text)
    updates = {}
    if parsed.get("fixed_monthly") is not None:
        updates["fixed_monthly"] = parsed["fixed_monthly"]
    if parsed.get("savings_amount") is not None:
        updates["savings_amount"] = parsed["savings_amount"]
        updates["savings_cadence"] = parsed.get("savings_cadence") or "biweekly"
    if updates:
        await prefs.set_prefs(session, **updates)

    p = await prefs.get_prefs(session)
    if p["fixed_monthly"] > 0 and p["savings_amount"] > 0:
        await set_kv(session, "onboarding_done", "1")
        return await llm.onboarding_done(p), True
    return await llm.onboarding_followup(p), False

"""User financial preferences (fixed costs, savings goal), stored in the KV table."""
from __future__ import annotations

from .db import get_kv, set_kv


def _f(v, default=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


async def get_prefs(session) -> dict:
    return {
        "fixed_monthly": _f(await get_kv(session, "cfg_fixed_monthly"), 0.0),
        "savings_amount": _f(await get_kv(session, "cfg_savings_amount"), 0.0),
        "savings_cadence": await get_kv(session, "cfg_savings_cadence", "biweekly"),
    }


async def set_prefs(session, fixed_monthly=None, savings_amount=None, savings_cadence=None) -> None:
    if fixed_monthly is not None:
        await set_kv(session, "cfg_fixed_monthly", str(fixed_monthly))
    if savings_amount is not None:
        await set_kv(session, "cfg_savings_amount", str(savings_amount))
    if savings_cadence is not None:
        await set_kv(session, "cfg_savings_cadence", savings_cadence)

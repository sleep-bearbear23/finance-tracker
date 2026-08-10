"""User financial preferences (fixed costs, savings goal, income profile), stored in the KV table."""
from __future__ import annotations

import json

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


async def get_income_profile(session) -> dict:
    """The forward-looking income picture Momo set in the starter pack."""
    raw = await get_kv(session, "cfg_upcoming")
    try:
        upcoming = json.loads(raw) if raw else []
        if not isinstance(upcoming, list):
            upcoming = []
    except Exception:
        upcoming = []
    return {
        "monthly_baseline": _f(await get_kv(session, "cfg_monthly_baseline"), 0.0),
        "upcoming": upcoming,
        "ytd_income": _f(await get_kv(session, "cfg_ytd_income"), 0.0),
        "cash_on_hand": _f(await get_kv(session, "cfg_cash_on_hand"), 0.0),
        "emergency_target": _f(await get_kv(session, "cfg_emergency_target"), 0.0),
        "total_debt": _f(await get_kv(session, "cfg_total_debt"), 0.0),
        "savings_balance": _f(await get_kv(session, "cfg_savings_balance"), 0.0),
    }


async def set_income_profile(session, data: dict) -> None:
    scalar = {
        "cfg_monthly_baseline": data.get("monthly_baseline"),
        "cfg_ytd_income": data.get("ytd_income"),
        "cfg_cash_on_hand": data.get("cash_on_hand"),
        "cfg_emergency_target": data.get("emergency_target"),
        "cfg_total_debt": data.get("total_debt"),
        "cfg_savings_balance": data.get("savings_balance"),
    }
    for k, v in scalar.items():
        if v is not None:
            await set_kv(session, k, str(v))
    up = data.get("upcoming")
    if up is not None:
        # keep only clean {amount, when, note} rows
        clean = []
        for u in (up if isinstance(up, list) else []):
            try:
                amt = float(u.get("amount"))
            except (TypeError, ValueError, AttributeError):
                continue
            if amt > 0:
                clean.append({"amount": amt, "when": u.get("when"), "note": u.get("note")})
        await set_kv(session, "cfg_upcoming", json.dumps(clean))

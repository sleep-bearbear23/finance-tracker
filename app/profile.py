"""Ingest the [[PROFILE]] starter-pack block Momo pastes in from the survey, and store it
as the budgeting inputs (fixed costs, savings goal, income baseline + booked-gig pipeline)."""
from __future__ import annotations

import json

from . import prefs
from .db import set_kv

MARKER = "[[PROFILE]]"


def parse(text: str) -> dict | None:
    """Pull the JSON object out of a pasted starter-pack block, else None."""
    if not text or MARKER not in text:
        return None
    body = text[text.find(MARKER) + len(MARKER):].strip()
    body = body.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        d = json.loads(body)
    except Exception:
        return None
    return d if isinstance(d, dict) else None


def _fixed_total(data: dict):
    ft = data.get("fixed_total")
    if ft is not None:
        try:
            return float(ft)
        except (TypeError, ValueError):
            pass
    fx = data.get("fixed") or {}
    if isinstance(fx, dict):
        try:
            return sum(float(v or 0) for v in fx.values())
        except (TypeError, ValueError):
            return None
    return None


async def apply(session, data: dict) -> dict:
    """Store the starter pack, mark onboarding done, and return a summary for the ack."""
    fixed_total = _fixed_total(data)
    await prefs.set_prefs(
        session,
        fixed_monthly=fixed_total,
        savings_amount=data.get("savings_amount"),
        savings_cadence=data.get("savings_cadence"),
    )
    await prefs.set_income_profile(session, data)
    await set_kv(session, "onboarding_done", "1")
    return summarize(data, fixed_total)


def summarize(data: dict, fixed_total=None) -> dict:
    if fixed_total is None:
        fixed_total = _fixed_total(data)
    up = data.get("upcoming") or []
    n_gigs = sum(1 for u in up if isinstance(u, dict) and u.get("amount"))
    try:
        gig_sum = sum(float(u.get("amount") or 0) for u in up if isinstance(u, dict))
    except (TypeError, ValueError):
        gig_sum = 0.0
    return {
        "ytd_income": data.get("ytd_income") or 0,
        "monthly_baseline": data.get("monthly_baseline") or 0,
        "fixed_total": fixed_total or 0,
        "savings_amount": data.get("savings_amount") or 0,
        "savings_cadence": data.get("savings_cadence") or "monthly",
        "n_gigs": n_gigs,
        "gig_sum": gig_sum,
        "cash_on_hand": data.get("cash_on_hand") or 0,
    }

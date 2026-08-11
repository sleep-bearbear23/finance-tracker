"""Talk to the 機房 (control room) — Windland's admin LINE channel.

秀琴阿姨 keeps her own voice for money talk with Momo. Machine chatter (import finished,
job failed, deploy live) belongs in the control room instead, so her persona stays clean.
Fire-and-forget: the control room being down must never affect her own work.
"""
from __future__ import annotations

import os

import httpx

# Windland's public URL + the same LOG_KEY it uses to gate its admin endpoints.
OPS_URL = os.environ.get("OPS_URL", "").rstrip("/")
OPS_KEY = os.environ.get("OPS_KEY", "")


async def say(text: str) -> None:
    """Post one line to the control room. Silent on any failure, by design."""
    if not (OPS_URL and OPS_KEY):
        return
    try:
        async with httpx.AsyncClient(timeout=8) as c:
            await c.post(f"{OPS_URL}/ops_say", params={"key": OPS_KEY},
                         json={"from": "秀琴阿姨", "text": text})
    except Exception as e:  # never let ops reporting break the finance bot
        print(f"[opsroom] {e!r}")

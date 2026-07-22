"""LINE Messaging API: signature verification + sending messages."""
from __future__ import annotations

import base64
import hashlib
import hmac

import httpx

from .config import settings

_PUSH = "https://api.line.me/v2/bot/message/push"
_REPLY = "https://api.line.me/v2/bot/message/reply"


def verify_signature(body: bytes, signature: str) -> bool:
    mac = hmac.new(settings.LINE_CHANNEL_SECRET.encode(), body, hashlib.sha256).digest()
    expected = base64.b64encode(mac).decode()
    return hmac.compare_digest(expected, signature or "")


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {settings.LINE_CHANNEL_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }


def _chunks(text: str):
    # LINE caps a text message at 5000 chars.
    for i in range(0, len(text), 4900):
        yield text[i:i + 4900]


async def push(user_id: str, text: str) -> None:
    if not user_id:
        return
    messages = [{"type": "text", "text": c} for c in _chunks(text)][:5]
    async with httpx.AsyncClient(timeout=30) as c:
        await c.post(_PUSH, headers=_headers(), json={"to": user_id, "messages": messages})


async def reply(reply_token: str, text: str) -> None:
    messages = [{"type": "text", "text": c} for c in _chunks(text)][:5]
    async with httpx.AsyncClient(timeout=30) as c:
        await c.post(_REPLY, headers=_headers(), json={"replyToken": reply_token, "messages": messages})

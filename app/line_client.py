"""LINE Messaging API: signature verification + sending messages."""
from __future__ import annotations

import base64
import hashlib
import hmac
import re

import httpx

from .config import settings

_PUSH = "https://api.line.me/v2/bot/message/push"
_REPLY = "https://api.line.me/v2/bot/message/reply"
_CONTENT = "https://api-data.line.me/v2/bot/message/{}/content"


def _strip_md(text: str) -> str:
    """LINE shows raw markdown as literal characters, so scrub it before sending.
    Removes **bold**/__ markers and any leading bullet/heading markers per line."""
    text = text.replace("**", "").replace("__", "")
    out = []
    for line in text.split("\n"):
        line = re.sub(r"^\s*[-*•·]\s+", "", line)   # bullet markers
        line = re.sub(r"^\s*#{1,6}\s+", "", line)    # markdown headings
        out.append(line)
    return "\n".join(out)


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
    messages = [{"type": "text", "text": c} for c in _chunks(_strip_md(text))][:5]
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(_PUSH, headers=_headers(), json={"to": user_id, "messages": messages})
    if r.status_code >= 300:
        print(f"[line] push failed {r.status_code}: {r.text}")


async def get_content(message_id: str) -> tuple[bytes, str]:
    """Download an image (or other media) the user sent, via the LINE content endpoint."""
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.get(
            _CONTENT.format(message_id),
            headers={"Authorization": f"Bearer {settings.LINE_CHANNEL_ACCESS_TOKEN}"},
        )
    return r.content, r.headers.get("content-type", "image/jpeg").split(";")[0].strip()


async def reply(reply_token: str, text: str) -> None:
    messages = [{"type": "text", "text": c} for c in _chunks(_strip_md(text))][:5]
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(_REPLY, headers=_headers(), json={"replyToken": reply_token, "messages": messages})
    if r.status_code >= 300:
        print(f"[line] reply failed {r.status_code}: {r.text}")

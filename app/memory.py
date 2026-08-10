"""Conversation memory — the last few things said, so replies land in context."""
from __future__ import annotations

from sqlalchemy import delete, select

from .models import Message

_KEEP = 400  # rows retained; she only reads the last handful


async def remember(session, role: str, content: str) -> None:
    if not content:
        return
    session.add(Message(role=role, content=content[:4000]))
    await session.commit()
    # occasional trim so the table doesn't grow forever
    newest = (await session.execute(select(Message.id).order_by(Message.id.desc()).limit(1))).scalar()
    if newest and newest % 50 == 0:
        await session.execute(delete(Message).where(Message.id < newest - _KEEP))
        await session.commit()


async def recent(session, n: int = 8) -> str:
    """Last n turns, oldest→newest, as '默默：…' / '阿姨：…' lines."""
    rows = (await session.execute(
        select(Message).order_by(Message.id.desc()).limit(n)
    )).scalars().all()
    rows.reverse()
    out = []
    for m in rows:
        who = "默默" if m.role == "user" else "阿姨"
        out.append(f"{who}：{m.content}")
    return "\n".join(out)

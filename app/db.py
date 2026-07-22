"""Async SQLAlchemy engine, session factory, and KV helpers."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from .config import settings
from .models import KV, Base


def _normalize(url: str) -> str:
    # Railway hands out postgres:// or postgresql://; asyncpg needs the +asyncpg driver.
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    if url.startswith("postgresql://") and "+asyncpg" not in url:
        url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
    # asyncpg rejects libpq-style ?sslmode=… params.
    if "?sslmode=" in url:
        url = url.split("?sslmode=")[0]
    return url


engine = create_async_engine(_normalize(settings.DATABASE_URL), pool_pre_ping=True)
Session = async_sessionmaker(engine, expire_on_commit=False)


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_kv(session, key: str, default: str | None = None) -> str | None:
    row = await session.get(KV, key)
    return row.value if row else default


async def set_kv(session, key: str, value: str) -> None:
    row = await session.get(KV, key)
    if row:
        row.value = value
    else:
        session.add(KV(key=key, value=value))
    await session.commit()

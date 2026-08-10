"""SimpleFIN Bridge client: claim the access url once, then poll transactions."""
from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone

import httpx

from . import classify
from .config import TZ, now, settings
from .db import get_kv, set_kv
from .models import Account, Transaction

_ACCESS_KEY = "simplefin_access_url"
_INIT_KEY = "initialized"


async def _resolve_access_url(session) -> str | None:
    """Return a usable access url, claiming the one-time setup token if needed."""
    url = await get_kv(session, _ACCESS_KEY)
    if url:
        return url
    if settings.SIMPLEFIN_ACCESS_URL:
        await set_kv(session, _ACCESS_KEY, settings.SIMPLEFIN_ACCESS_URL)
        return settings.SIMPLEFIN_ACCESS_URL
    if settings.SIMPLEFIN_SETUP_TOKEN:
        claim_url = base64.b64decode(settings.SIMPLEFIN_SETUP_TOKEN).decode()
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(claim_url)
            r.raise_for_status()
            access = r.text.strip()
        await set_kv(session, _ACCESS_KEY, access)
        return access
    return None


def _split_auth(access_url: str) -> tuple[str, tuple[str, str] | None]:
    # https://user:pass@host/path  ->  (https://host/path, (user, pass))
    if "@" not in access_url:
        return access_url, None
    scheme, rest = access_url.split("://", 1)
    creds, host = rest.split("@", 1)
    user, _, pw = creds.partition(":")
    return f"{scheme}://{host}", (user, pw)


async def ingest(session) -> int:
    """Pull recent transactions. Returns the number of new spend charges awaiting context."""
    access = await _resolve_access_url(session)
    if not access:
        return 0
    base, auth = _split_auth(access)
    start = int((now() - timedelta(days=settings.BACKFILL_DAYS)).timestamp())

    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.get(f"{base}/accounts", params={"start-date": start, "pending": 1}, auth=auth)
        r.raise_for_status()
        payload = r.json()

    initialized = (await get_kv(session, _INIT_KEY)) == "1"
    new_needs = 0

    for acct in payload.get("accounts", []):
        a = await session.get(Account, acct["id"])
        if not a:
            a = Account(id=acct["id"])
            session.add(a)
        a.name = acct.get("name", "")
        a.org = (acct.get("org") or {}).get("name", "") if isinstance(acct.get("org"), dict) else ""
        try:
            a.balance = float(acct.get("balance", 0) or 0)
        except (TypeError, ValueError):
            a.balance = 0.0
        if acct.get("balance-date"):
            a.balance_date = datetime.fromtimestamp(int(acct["balance-date"]), tz=timezone.utc).astimezone(TZ)

        for tx in acct.get("transactions", []):
            if await session.get(Transaction, tx["id"]):
                continue  # already have it
            try:
                amount = float(tx.get("amount", 0) or 0)
            except (TypeError, ValueError):
                amount = 0.0
            posted = None
            if tx.get("posted"):
                posted = datetime.fromtimestamp(int(tx["posted"]), tz=timezone.utc).astimezone(TZ)
            desc = tx.get("description", "") or tx.get("payee", "") or ""
            status, category, note = await classify.classify(session, desc, amount, backfill=not initialized)
            if status == "needs_context":
                new_needs += 1

            session.add(Transaction(
                id=tx["id"], account_id=acct["id"], amount=amount,
                merchant_desc=desc, posted_at=posted, category=category, note=note, status=status,
            ))

    await session.commit()
    if not initialized:
        await set_kv(session, _INIT_KEY, "1")
    return new_needs

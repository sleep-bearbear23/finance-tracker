"""SimpleFIN Bridge client: claim the access url once, then poll transactions."""
from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone

import httpx

from sqlalchemy import select

from . import classify
from .config import TZ, aware, now, settings
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
            new_needs += await absorb(session, acct["id"], tx, initialized)

    await session.commit()
    if not initialized:
        await set_kv(session, _INIT_KEY, "1")
    return new_needs


async def absorb(session, acct_id: str, tx: dict, initialized: bool) -> int:
    """File one feed transaction: insert, upgrade a pending twin, or skip a known row.

    Split out of :func:`ingest` so the dating rules are testable without a bank."""
    try:
        amount = float(tx.get("amount", 0) or 0)
    except (TypeError, ValueError):
        amount = 0.0
    posted = None
    if tx.get("posted"):
        posted = datetime.fromtimestamp(int(tx["posted"]), tz=timezone.utc).astimezone(TZ)
    desc = tx.get("description", "") or tx.get("payee", "") or ""

    have = await session.get(Transaction, tx["id"])
    if have is not None:
        # A pending row carries no posted date; the bank fills it in when the
        # charge settles. Sixteen rows — including a $1,400 Zelle from a work
        # payer — sat with posted_at NULL forever because this upgrade never
        # happened, and a row with no date belongs to no period.
        if have.posted_at is None and posted is not None:
            have.posted_at = posted
        return 0

    # Some banks settle a pending charge under a NEW id. Without this check the
    # same money lands twice: once undated, once dated. If an undated row on this
    # account matches on amount and description within a week, this IS that row —
    # keep everything Momo taught it and move it onto the settled identity.
    twin = None
    if posted is not None:
        twin = next(
            (t for t in (await session.execute(
                select(Transaction).where(
                    Transaction.account_id == acct_id,
                    Transaction.posted_at.is_(None)))).scalars()
             if abs(t.amount - amount) < 0.005
             and (t.merchant_desc or "")[:24] == desc[:24]
             and t.created_at is not None
             and abs((posted - aware(t.created_at)).days) <= 7),
            None)
    if twin is not None:
        session.add(Transaction(
            id=tx["id"], account_id=acct_id, amount=amount,
            merchant_desc=desc, posted_at=posted,
            category=twin.category, note=twin.note, status=twin.status,
            inflow_kind=twin.inflow_kind, reimbursable=twin.reimbursable,
            project=twin.project, claim=twin.claim,
            effective_at=twin.effective_at, nets_txn_id=twin.nets_txn_id,
            created_at=twin.created_at,
        ))
        await session.delete(twin)
        return 0

    status, category, note, inflow = await classify.classify(
        session, desc, amount, backfill=not initialized)

    session.add(Transaction(
        id=tx["id"], account_id=acct_id, amount=amount,
        merchant_desc=desc, posted_at=posted, category=category, note=note,
        status=status, inflow_kind=inflow,
    ))
    return 1 if status == "needs_context" else 0

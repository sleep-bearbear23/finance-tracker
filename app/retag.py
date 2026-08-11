"""One-time passes that bring 19 months of history onto the new taxonomy.

Two jobs, both idempotent and both flagged in KV so a redeploy doesn't redo them:

  retag()   old English category names -> taxonomy ids, and a fresh guess at
            everything that was never categorised at all.
  net_refunds()  match each refund back to the charge it reverses, so the refund
            inherits that charge's category and the bucket sums itself out.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import timedelta

from sqlalchemy import select

from . import taxonomy as T
from .config import aware
from .db import get_kv, set_kv
from .models import MerchantMemory, Transaction

RETAG_FLAG = "taxonomy_v1"
NETTING_FLAG = "refund_netting_v2"

#: how far back a refund may reach to find its charge. Momo's Amazon returns land
#: one to two months after the order; 120 days covers the long tail without
#: matching an unrelated purchase from last season.
REFUND_LOOKBACK = timedelta(days=120)


async def retag(session) -> dict[str, int]:
    """Move every transaction onto a taxonomy id. Returns counts of what happened."""
    if await get_kv(session, RETAG_FLAG) == "1":
        return {}

    n_mapped = n_guessed = n_unknown = n_ignored = 0
    rows = (await session.execute(select(Transaction))).scalars().all()
    for t in rows:
        old = t.category
        if old in T.ALL:
            continue  # already on the new scheme
        new = T.from_legacy(old, t.merchant_desc or "")
        if new is None and old:
            # a legacy name we have no mapping for — re-guess rather than guess-and-hope
            new = T.guess(t.merchant_desc or "")
        if new is None:
            new = T.guess(t.merchant_desc or "")
            if new:
                n_guessed += 1
        elif old:
            n_mapped += 1
        else:
            n_guessed += 1
        t.category = new
        if new is None:
            n_unknown += 1
        elif T.is_skip(new) and t.status not in ("reconciled",):
            t.status = "ignored"
            n_ignored += 1

    # merchant memory speaks the same language, or she'll re-teach every merchant
    n_mem = 0
    for m in (await session.execute(select(MerchantMemory))).scalars().all():
        if m.category and m.category not in T.ALL:
            m.category = T.from_legacy(m.category, m.key or "") or T.guess(m.key or "")
            n_mem += 1

    await set_kv(session, RETAG_FLAG, "1")
    await session.commit()
    return {"mapped": n_mapped, "guessed": n_guessed, "unknown": n_unknown,
            "ignored": n_ignored, "memory": n_mem}


def _match(refund, charges: list) -> object | None:
    """Pick the charge a refund reverses: same merchant, earlier, closest amount.

    Exact amount wins outright — that is a plain return. Otherwise take the nearest
    charge that is at least as big (a partial return of a larger order), preferring
    the most recent. A refund with no plausible charge stays unmatched, which is the
    honest answer; it shows up in the inbox instead of quietly landing somewhere."""
    amt = refund.amount            # positive
    rd = aware(refund.posted_at or refund.created_at)
    if rd is None:
        return None
    window = rd - REFUND_LOOKBACK

    exact, partial = [], []
    for c in charges:
        cd = aware(c.posted_at or c.created_at)
        if cd is None or cd > rd or cd < window:
            continue
        mag = -c.amount
        if abs(mag - amt) < 0.005:
            exact.append((cd, c))
        elif mag > amt:
            partial.append((cd, c))
    if exact:
        return max(exact, key=lambda p: p[0])[1]
    if partial:
        # smallest charge that still covers the refund, tie-broken by recency
        return min(partial, key=lambda p: (-p[1].amount - amt, -p[0].timestamp()))[1]
    return None


async def net_refunds(session, force: bool = False) -> dict[str, int]:
    """Give every unexplained credit the category of the charge it reverses."""
    if not force and await get_kv(session, NETTING_FLAG) == "1":
        return {}

    rows = (await session.execute(select(Transaction))).scalars().all()
    by_brand: dict[str, list] = defaultdict(list)
    for t in rows:
        if t.amount < 0:
            by_brand[T.brand_key(t.merchant_desc)].append(t)

    matched = by_marker = unmatched = 0
    for t in rows:
        if t.amount <= 0 or t.nets_txn_id:
            continue
        if t.inflow_kind == T.PAY or t.status == "income":
            continue  # real pay, hands off
        if (t.category or "") in ("tax", "transfer") or t.status == "reconciled":
            continue

        charge = _match(t, by_brand.get(T.brand_key(t.merchant_desc), []))
        if charge is not None:
            t.nets_txn_id = charge.id
            t.effective_at = charge.posted_at or charge.created_at
            t.category = charge.category or T.guess(t.merchant_desc)
            t.inflow_kind = t.inflow_kind or T.REFUND
            t.status = "auto"
            matched += 1
        elif T.looks_like_return(t.merchant_desc):
            # The statement says it's a return but the original charge is outside our
            # window (or was never imported). It still has to come off the bucket —
            # the category comes from the merchant instead of from the charge.
            t.category = T.guess(t.merchant_desc) or t.category
            t.inflow_kind = t.inflow_kind or T.REFUND
            t.status = "auto"
            by_marker += 1
        else:
            unmatched += 1

    await set_kv(session, NETTING_FLAG, "1")
    await session.commit()
    return {"matched": matched, "by_marker": by_marker, "unmatched": unmatched}

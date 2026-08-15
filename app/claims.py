"""Money Momo fronted that someone else owes her back — and the credit that settles it.

Two shapes, one problem. A production says "expense it and we'll reimburse", so she pays for
the taxi and the set lunch out of her own pocket. Amazon takes a return, so a charge from
three weeks ago is coming back as a credit. In both cases real money left, real money is due
to return, and in between there is a thing that needs chasing.

Momo: "there need to be a transaction record for each project, and updated refund and
reimbursement status, and whether she needs to rush me for asking reimbursement or asking
amazon about refund etc. That also means she need to detect some income transaction from
vendors and ask about whether it's a refund for something, and do matching with numbers too
(cuz manual identification could be crazy)."

The last clause is the design constraint. Manual identification does not scale, so the
matcher runs first and a human is only asked where the numbers are ambiguous. It is
deliberately conservative in the same way :mod:`app.projects` is: an exact amount and a
plausible interval, one claim per credit, and anything it is not sure about becomes a
question rather than a silent pairing.

States live on the transaction (``claim``):

    todo   she fronted it and has not asked for it back yet   ← chase HER
    sent   asked; now it is the other side's turn             ← chase THEM
    paid   the credit arrived and is linked                   ← done
    wont   she has decided to eat it                          ← a real business cost

Nothing here touches the fortnight. A reimbursable cost is 工作 and 工作 is not in the
allowance, so a taxi to set never competed with her groceries in the first place — that was
the bug this whole area exists because of.
"""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from . import budget
from . import taxonomy as T
from .config import now
from .models import Transaction

#: how close a credit has to be to the charge it repays
TOLERANCE = 0.02
#: and how long a reimbursement may plausibly take to come back
WINDOW_DAYS = 180

#: after this long unclaimed, she is the bottleneck and should be nudged
NUDGE_TODO_DAYS = 10
#: after this long since claiming, they are, and it is worth a chase
NUDGE_SENT_DAYS = 30

LABEL = {"todo": "還沒去要", "sent": "已經要了，等對方",
         "paid": "收回來了", "wont": "自己吸收"}


def state_of(t) -> str | None:
    """What a row's claim state is, filling in the obvious default."""
    if getattr(t, "claim", None):
        return t.claim
    if getattr(t, "reimbursable", None) and T.is_work(t.category):
        return "todo"           # she said someone owes it and nothing says otherwise
    return None


async def outstanding(session) -> list[dict]:
    """Everything she has fronted and not got back, oldest first — the chase list."""
    rows = (await session.execute(select(Transaction))).scalars().all()
    today = now().date()
    out = []
    for t in rows:
        if t.amount >= 0:
            continue
        st = state_of(t)
        if st not in ("todo", "sent"):
            continue
        d = budget.eff_date(t)
        age = (today - d).days if d else 0
        out.append({
            "id": t.id, "date": d.isoformat() if d else None,
            "merchant": (t.merchant_desc or "")[:40], "amount": round(abs(t.amount), 2),
            "project": t.project, "claim": st, "label": LABEL[st], "age": age,
            # whose move is it? "todo" means she has not asked yet, so the nudge is hers.
            "who": "me" if st == "todo" else "them",
            "nudge": age >= (NUDGE_TODO_DAYS if st == "todo" else NUDGE_SENT_DAYS),
        })
    out.sort(key=lambda x: -x["age"])
    return out


def _candidates(rows: list, credit) -> list:
    """Charges a given credit could plausibly be repaying."""
    when = budget.eff_date(credit)
    hits = []
    for t in rows:
        if t.amount >= 0 or t.nets_txn_id:
            continue
        if state_of(t) not in ("todo", "sent"):
            continue
        if abs(abs(t.amount) - credit.amount) > TOLERANCE:
            continue
        d = budget.eff_date(t)
        if when and d and not (0 <= (when - d).days <= WINDOW_DAYS):
            continue          # a credit cannot repay a charge that came after it
        hits.append(t)
    return hits


async def match(session, apply: bool = True) -> dict:
    """Pair incoming credits with the costs they repay.

    Only settles where exactly ONE outstanding claim has that amount. Two identical fares
    on the same shoot are genuinely ambiguous, and picking either would mark the wrong one
    done — so those come back as questions for 陳會計 to ask.
    """
    rows = (await session.execute(select(Transaction))).scalars().all()
    charges = [t for t in rows if t.amount < 0]
    settled, asks = [], []

    for c in rows:
        if c.amount <= 0 or c.nets_txn_id:
            continue
        if budget.is_income(c):
            continue                       # real pay is not a refund
        if getattr(c, "inflow_kind", None) == T.PAY:
            continue
        hits = _candidates(charges, c)
        if not hits:
            continue
        if len(hits) > 1:
            asks.append({
                "credit": c.id, "amount": round(c.amount, 2),
                "desc": (c.merchant_desc or "")[:40],
                "options": [{"id": t.id, "merchant": (t.merchant_desc or "")[:40],
                             "date": (budget.eff_date(t) or "").isoformat()
                             if budget.eff_date(t) else None,
                             "project": t.project} for t in hits[:5]],
            })
            continue
        t = hits[0]
        if apply:
            c.nets_txn_id = t.id
            c.category = t.category
            c.inflow_kind = c.inflow_kind or (
                T.REIMBURSE_WORK if T.is_work(t.category) else T.REFUND)
            c.effective_at = t.posted_at or t.created_at
            c.status = "auto"
            t.claim = "paid"
        settled.append({"credit": c.id, "charge": t.id,
                        "amount": round(c.amount, 2),
                        "merchant": (t.merchant_desc or "")[:40],
                        "project": t.project})
    if apply and (settled or asks):
        await session.commit()
    return {"settled": settled, "ask": asks,
            "n_settled": len(settled), "n_ask": len(asks)}


async def summary(session) -> dict:
    out = await outstanding(session)
    mine = [x for x in out if x["who"] == "me"]
    theirs = [x for x in out if x["who"] == "them"]
    return {
        "items": out,
        "total": round(sum(x["amount"] for x in out), 2),
        "mine_total": round(sum(x["amount"] for x in mine), 2),
        "theirs_total": round(sum(x["amount"] for x in theirs), 2),
        "nudge": [x for x in out if x["nudge"]],
        "note": ("你先墊、還沒拿回來的錢。「還沒去要」是等你開口，「已經要了」是等對方——"
                 "分開看才知道要催誰。這些都算工作支出，本來就不會從你每天能花的錢裡扣。"),
    }

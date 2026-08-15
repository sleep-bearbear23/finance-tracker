"""One record per job, assembled from the three places that already describe it.

Momo: "since so much of my earning is related to project, we should just start a tab that
tracks project where we can just document more complete information about each project.
It's a good record to have."

She is right that it is one object seen three ways, and that nothing in the app knew that:

    發票封存      client, shoot dates, day rate, days, prep/overtime, total
    待收款        stage, expected landing, how late, how much to trust it
    入帳          the deposit, when it actually cleared

So the day rate was computed from one list, 該催的錢 from another, and the invoice archive
sat in a folder answering to neither. A gig she shot in May appears in all three and was
three unrelated rows.

This DERIVES rather than replaces. There is no migration and no new source of truth: the
archive, the pending list and the ledger stay exactly where they are, and a project is what
you get when you line them up on the amount. A record only exists here because it exists
there, so nothing can drift out of sync with the money.

The overlay in ``cfg_projects`` is for the fields only Momo can supply — what her role was,
how the shoot went, whether she would work for them again. Those are not derivable from a
bank feed and they are most of why she wants the record.
"""
from __future__ import annotations

import json
import re
from datetime import date, timedelta

from . import budget
from . import facts as F
from . import prefs
from . import seed_invoices as SI
from .config import now
from .db import get_kv, set_kv

OVERLAY_KEY = "cfg_projects"

#: How close a deposit has to be to an invoice total to count as that invoice being paid.
#: Exact to the cent in her data; the tolerance is for a wire fee shaving a dollar off.
MATCH_TOLERANCE = 2.0
#: and how far either side of the expected landing date to look
MATCH_WINDOW = 120


def slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s[:40] or "project"


async def overlay(session) -> dict[str, dict]:
    raw = await get_kv(session, OVERLAY_KEY)
    try:
        rows = json.loads(raw) if raw else []
    except (TypeError, ValueError):
        rows = []
    return {r["id"]: r for r in rows if isinstance(r, dict) and r.get("id")}


async def annotate(session, pid: str, **fields) -> dict:
    """Momo's own notes on a job — role, how it went, whether she'd go back."""
    cur = await overlay(session)
    row = cur.get(pid, {"id": pid})
    row.update({k: v for k, v in fields.items() if v is not None})
    cur[pid] = row
    await set_kv(session, OVERLAY_KEY, json.dumps(list(cur.values()), ensure_ascii=False))
    return row


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9一-鿿]+", "", (s or "").lower())


def _pairs(a: str, b: str) -> bool:
    """Do these two describe the same job? Names drift between an invoice and the note she
    typed into LINE — 「The Lady in the Blue Dress」 vs 「Woman in Blue Dress (Verticals)」 —
    so this compares on the distinctive words rather than the whole string."""
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return False
    if na in nb or nb in na:
        return True
    wa = {w for w in re.split(r"[^a-z0-9一-鿿]+", (a or "").lower()) if len(w) > 3}
    wb = {w for w in re.split(r"[^a-z0-9一-鿿]+", (b or "").lower()) if len(w) > 3}
    return len(wa & wb) >= 2


async def build(session, f: F.Facts | None = None) -> list[dict]:
    """Every job on record, newest first."""
    f = f or await F.build(session)
    today = now().date()
    invoices = await SI.invoices(session)
    pend = await prefs.pending_invoices(session)
    over = await overlay(session)

    # deposits that could be someone paying an invoice
    income = []
    for t in f.txns:
        if not budget.is_income(t):
            continue
        d = budget.eff_date(t)
        if d:
            income.append({"date": d, "amount": round(t.amount, 2),
                           "desc": (t.merchant_desc or "")[:60]})

    used_pend, used_inc, out = set(), set(), []

    for inv in invoices:
        total = float(inv.get("total") or 0)
        p = {
            "id": slug(inv.get("project") or inv.get("num") or ""),
            "name": inv.get("project") or inv.get("num"),
            "client": inv.get("client"),
            "invoice": inv.get("num"), "invoiced_on": inv.get("date"),
            "rate": inv.get("rate"), "days": inv.get("days"),
            "day_total": inv.get("day_total"), "extras": inv.get("extras"),
            "total": round(total, 2), "kind": inv.get("kind") or "shoot",
            "note": inv.get("note"),
        }

        # still owed?
        for i, q in enumerate(pend):
            if i in used_pend:
                continue
            if abs(float(q.get("amount") or 0) - total) < MATCH_TOLERANCE or \
                    _pairs(inv.get("match") or inv.get("project") or "", q.get("note") or ""):
                land = prefs.landing(q)
                p.update({
                    "stage": prefs.stage_of(q), "owed": round(float(q.get("amount") or 0), 2),
                    "expect_on": q.get("expect_on"), "wrapped_on": q.get("wrapped_on"),
                    "lands": land.isoformat() if land else None,
                    "late_days": max(0, (today - land).days) if land else 0,
                    "confidence": round(prefs.confidence(q, today), 2),
                })
                used_pend.add(i)
                break

        # or already in the bank? Matched on amount, so it is a guess and says so — and
        # the window runs FORWARD from the invoice, because a deposit four months before
        # she billed for the job is some other job's money.
        if not p.get("owed"):
            ref = inv.get("date")
            for j, dep in enumerate(income):
                if j in used_inc or abs(dep["amount"] - total) >= MATCH_TOLERANCE:
                    continue
                if ref:
                    gap = (dep["date"] - date.fromisoformat(ref)).days
                    if not (-7 <= gap <= MATCH_WINDOW):
                        continue
                p.update({"paid_on": dep["date"].isoformat(), "paid": dep["amount"],
                          "paid_match": "amount", "paid_desc": dep["desc"]})
                used_inc.add(j)
                break

        p["status"] = ("已收" if p.get("paid") else
                       {"booked": "已接", "wrapped": "已殺青", "invoiced": "已開發票"}
                       .get(p.get("stage"), "結案"))
        out.append(p)

    # jobs she has booked but not invoiced yet — they have no archive entry by definition
    for i, q in enumerate(pend):
        if i in used_pend:
            continue
        land = prefs.landing(q)
        out.append({
            "id": slug(q.get("note") or ""), "name": q.get("note"),
            "client": None, "invoice": None, "invoiced_on": None,
            "rate": None, "days": q.get("days"),
            "total": round(float(q.get("amount") or 0), 2), "kind": "shoot",
            "stage": prefs.stage_of(q), "owed": round(float(q.get("amount") or 0), 2),
            "expect_on": q.get("expect_on"), "wrapped_on": q.get("wrapped_on"),
            "lands": land.isoformat() if land else None,
            "late_days": max(0, (today - land).days) if land else 0,
            "confidence": round(prefs.confidence(q, today), 2),
            "status": {"booked": "已接", "wrapped": "已殺青", "invoiced": "已開發票"}
                      .get(prefs.stage_of(q), "已接"),
            "when": q.get("when"),
        })

    for p in out:
        p.update({k: v for k, v in (over.get(p["id"]) or {}).items() if k != "id"})
    out.sort(key=lambda p: (p.get("invoiced_on") or p.get("wrapped_on")
                            or p.get("when") or ""), reverse=True)
    return out


async def summary(session, f: F.Facts | None = None) -> dict:
    """The tab's header: what she has done, what it paid, and what is still out there."""
    rows = await build(session, f)
    shoot = [p for p in rows if p.get("days") and p.get("rate")]
    owed = [p for p in rows if p.get("owed")]
    days = sum(int(p["days"]) for p in shoot)
    fees = sum(float(p.get("day_total") or 0) for p in shoot)
    extras = sum(float(p.get("extras") or 0) for p in rows)
    clients: dict[str, dict] = {}
    for p in rows:
        c = p.get("client")
        if not c:
            continue
        row = clients.setdefault(c, {"client": c, "jobs": 0, "days": 0, "total": 0.0,
                                     "rates": []})
        row["jobs"] += 1
        row["days"] += int(p.get("days") or 0)
        row["total"] += float(p.get("total") or 0)
        if p.get("rate"):
            row["rates"].append({"when": p.get("invoiced_on"), "rate": p["rate"]})
    for row in clients.values():
        row["rates"].sort(key=lambda x: x["when"] or "")
        row["total"] = round(row["total"], 2)
        row["day_rate"] = (round(row["total"] / row["days"], 2) if row["days"] else None)
    return {
        "projects": rows,
        "n": len(rows), "shoot_days": days,
        "day_fees": round(fees, 2), "extras": round(extras, 2),
        "billed": round(sum(float(p.get("total") or 0) for p in rows), 2),
        "owed_total": round(sum(float(p["owed"]) for p in owed), 2),
        "owed_n": len(owed),
        "avg_rate": round(fees / days, 2) if days else None,
        "clients": sorted(clients.values(), key=lambda c: -c["days"]),
        "note": ("每個案子只有一筆紀錄，發票、待收、入帳都掛在上面。日薪、要接多少、"
                 "該催的錢都是從這裡讀出來的，所以不會有三份對不起來的名單。"),
    }

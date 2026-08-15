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
    """Keep CJK. Stripping it collapsed 「藍衣女子」 and every other Chinese-only name to the
    literal string "project", so they would all have shared one record."""
    s = re.sub(r"[^a-z0-9一-鿿ぁ-ゟ゠-ヿ]+", "-", (text or "").lower()).strip("-")
    return s[:48] or "project"


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


#: below this a candidate is not the same job, and a fresh record is the honest answer
MATCH_FLOOR = 20
#: how far ahead the winner has to be before we believe it rather than asking her. Under
#: this, several jobs are equally plausible and picking one silently files a taxi fare
#: against the wrong shoot — which is worse than a question.
MATCH_MARGIN = 10


def _score(a: str, b: str) -> int:
    """How strongly these two name the same job. Scored rather than boolean, because a
    boolean cannot choose between five jobs for the same client: 「AVIA 八月拍攝」 matched
    every Avia gig she has ever done, and one taxi fare showed up on all five of them."""
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return 0
    if na == nb:
        return 100
    if na in nb or nb in na:
        return 60 + min(30, min(len(na), len(nb)))
    wa = {w for w in re.split(r"[^a-z0-9一-鿿]+", (a or "").lower()) if len(w) > 3}
    wb = {w for w in re.split(r"[^a-z0-9一-鿿]+", (b or "").lower()) if len(w) > 3}
    shared = wa & wb
    if len(shared) >= 2:
        return 50 + 5 * len(shared)
    if shared:
        # One shared word is usually the CLIENT, not the job — 「AVIA 八月拍攝」 shares
        # exactly "avia" with all five Avia gigs and tells them apart not at all. Weak on
        # purpose, so it can never outrank a real name match.
        return 25
    # one distinctive token is a prefix of the other — 「AVIA」 inside 「AviaGames」. Real,
    # but weak: it identifies the CLIENT far more often than the job, so it scores below
    # anything else and only wins when nothing else matches at all.
    if any(len(x) >= 4 and len(y) >= 4 and (x.startswith(y) or y.startswith(x))
           for x in wa for y in wb):
        return 20
    return 0


def _pairs(a: str, b: str) -> bool:
    return _score(a, b) >= MATCH_FLOOR


async def resolve(session, text: str, f: F.Facts | None = None) -> str:
    """Turn whatever Momo called the job into the id the record is filed under.

    She says 「AVIA 八月拍攝」 in LINE; the invoice calls it 「AviaGames — August 2026」. If a
    charge is stored under her phrasing it hangs off nothing, so the cost never appears on
    the job and the whole point of tagging it is lost. Falls back to a slug of what she
    said, which at least groups her own charges together consistently.

    Returns ``{"id", "new", "options"}``. ``id`` is None when several jobs are equally
    plausible — 「AVIA 八月拍攝」 against five Avia gigs — and then ``options`` is the
    shortlist for 陳會計 to read back, because filing a taxi fare against the wrong shoot
    is worse than one more question."""
    want = (text or "").strip()
    if not want:
        return ""
    rows = await build(session, f)
    scored = []
    for p in rows:                      # newest first, so ties keep the current job
        sc = max(_score(want, p["id"]), _score(want, p.get("name") or ""),
                 _score(want, p.get("client") or "") - 10)
        if sc >= MATCH_FLOOR:
            scored.append((sc, p))
    scored.sort(key=lambda x: -x[0])
    if not scored:
        return {"id": slug(want), "new": True, "score": 0, "options": []}
    top, runner = scored[0], (scored[1] if len(scored) > 1 else None)
    if runner and top[0] - runner[0] < MATCH_MARGIN:
        return {"id": None, "new": False, "score": top[0],
                "options": [{"id": p["id"], "name": p.get("name"), "client": p.get("client"),
                             "when": p.get("invoiced_on") or p.get("when")}
                            for _sc, p in scored[:5]]}
    return {"id": top[1]["id"], "new": False, "score": top[0], "options": []}


async def build(session, f: F.Facts | None = None) -> list[dict]:
    """Every job on record, newest first."""
    f = f or await F.build(session)
    today = now().date()
    invoices = await SI.invoices(session)
    pend = await prefs.pending_invoices(session)
    over = await overlay(session)

    # Money she spent FOR a job. Kept off the fortnight entirely — 工作 is not in
    # `in_allowance` — and reported here instead, because a production's taxi fare is not
    # a fact about how she is living.
    costs: dict[str, list] = {}
    for t in f.txns:
        if t.amount >= 0 or not getattr(t, "project", None):
            continue
        d = budget.eff_date(t)
        costs.setdefault(t.project, []).append({
            "date": d.isoformat() if d else None,
            "amount": round(abs(t.amount), 2),
            "merchant": (t.merchant_desc or "")[:40],
            "reimbursable": getattr(t, "reimbursable", None),
        })

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
        # Exact id only. Fuzzy matching here put one taxi fare on all five Avia jobs —
        # tools.resolve() canonicalises the name when the charge is written, which is the
        # one place a human is present to be asked.
        rows = costs.get(p["id"]) or []
        if rows:
            back = round(sum(c["amount"] for c in rows if c["reimbursable"]), 2)
            p["costs"] = sorted(rows, key=lambda c: c["date"] or "", reverse=True)
            p["cost_total"] = round(sum(c["amount"] for c in rows), 2)
            p["cost_reimbursable"] = back
            p["cost_own"] = round(p["cost_total"] - back, 2)
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
    spent = round(sum(float(p.get("cost_total") or 0) for p in rows), 2)
    claimable = round(sum(float(p.get("cost_reimbursable") or 0) for p in rows), 2)
    return {
        "projects": rows,
        "n": len(rows), "shoot_days": days,
        "spent_on_jobs": spent, "claimable": claimable,
        "own_cost": round(spent - claimable, 2),
        "day_fees": round(fees, 2), "extras": round(extras, 2),
        "billed": round(sum(float(p.get("total") or 0) for p in rows), 2),
        "owed_total": round(sum(float(p["owed"]) for p in owed), 2),
        "owed_n": len(owed),
        "avg_rate": round(fees / days, 2) if days else None,
        "clients": sorted(clients.values(), key=lambda c: -c["days"]),
        "note": ("每個案子只有一筆紀錄，發票、待收、入帳、為了它花的錢都掛在上面。"
                 "為了案子花的錢算工作支出，不會從你每天能花的錢裡扣——買的是什麼不重要，"
                 "重要的是那是誰的錢。"),
    }

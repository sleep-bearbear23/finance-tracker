"""The web dashboard: a token-gated window into 陳會計's data and her actual math.

Two faces, one page:
  • 總覽  — net worth trend, spending breakdowns, budget, pending invoices (visual).
  • 訓練輪 — how every number is computed, the full ledger, what she's learned (transparency).

Everything is read-only. Auth is a single shared secret (DASHBOARD_TOKEN); with it unset
the whole thing 503s so it can never be left open by accident.
"""
from __future__ import annotations

import hmac
import json
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import select

from . import (accounts as acct, allowance, analytics as AN, budget, categories,
               changelog as CL,
               export as EX, facts as F, fixed as FX, networth, period as P,
               prefs, projects as PJ, runway as RW, season as SE, stability as STAB,
               tax as TAX,
               taxonomy)
from .config import aware, now, settings
from .db import Session, get_kv
from .models import Account, MerchantMemory, Message, Snapshot, Transaction

router = APIRouter()
_HTML = (Path(__file__).parent / "dashboard.html")
_LABEL_HTML = (Path(__file__).parent / "label.html")
_SETTLE_HTML = (Path(__file__).parent / "settle.html")


def _cat_labels_json() -> str:
    """id -> 中文, injected into the page so the screen never shows a raw slug."""
    m = {cid: zh for cid, (zh, _t, _n) in taxonomy.CATEGORIES.items()}
    m["__other"] = "其他"      # the donut's rolled-up tail
    m["未分類"] = "未分類"
    return json.dumps(m, ensure_ascii=False)


def _cat_order_json() -> str:
    """The fixed hue order. Colour has to follow the CATEGORY, not the order a given
    page happened to encounter it in — 食 was rendering green on the overview and cyan on
    the 計畫 page because each page assigned hues as it went. Treatment groups are kept
    together so 固定 lines read as a family."""
    slots = dict(taxonomy.PALETTE_SLOT)
    for i, t in enumerate(taxonomy.TREATMENT_LABEL):
        slots.setdefault(t, i % 7)
    slots.setdefault("未分類", 6)
    slots.setdefault("__other", 6)
    return json.dumps(slots, ensure_ascii=False)


def _cat_options() -> list[dict]:
    """Category picker payload, grouped so the UI can show 固定 / 彈性 / 想要 … headers."""
    return [
        {"id": cid, "label": zh, "treatment": tr,
         "group": taxonomy.TREATMENT_LABEL[tr], "note": nt}
        for cid, (zh, tr, nt) in taxonomy.CATEGORIES.items()
    ]


def _authorized(request: Request) -> bool:
    tok = settings.DASHBOARD_TOKEN
    if not tok:
        return False
    given = (
        request.query_params.get("t")
        or request.cookies.get("dash")
        or request.headers.get("x-dash-token")
        or ""
    )
    return bool(given) and hmac.compare_digest(given, tok)


def _deny():
    return JSONResponse({"error": "unauthorized"}, status_code=403)


# ── the trend snapshot ───────────────────────────────────────────────
async def write_snapshot(session) -> None:
    """Upsert today's point so the net-worth / budget trend keeps growing."""
    nw = (await F.build(session)).nw
    try:
        b = await budget.status(session)
    except Exception:
        b = {"allowance": 0.0, "spent": 0.0, "income_period": 0.0}
    day = now().date().isoformat()
    row = (await session.execute(select(Snapshot).where(Snapshot.day == day))).scalar_one_or_none()
    if row is None:
        row = Snapshot(day=day)
        session.add(row)
    row.net_worth = nw["net"]
    row.assets = nw["assets"]
    row.debts = nw["debts"]
    row.cash = nw["spendable"]  # brokerage is not grocery money
    row.allowance = b.get("allowance", 0.0)
    row.spent = b.get("spent", 0.0)
    row.income_biweekly = b.get("income_period", 0.0)
    await session.commit()


# ── helpers ──────────────────────────────────────────────────────────
def _spend_ok(t) -> bool:
    return budget.is_spend(t)


async def _pending_block(session, nw):
    pend = await prefs.pending_invoices(session)
    items, total = [], 0.0
    today_ym = now().strftime("%Y-%m")
    for p in pend:
        amt = float(p.get("amount") or 0)
        total += amt
        items.append({
            "note": p.get("note") or "某案",
            "amount": amt,
            "when": p.get("when"),
            "overdue": bool(p.get("when") and str(p.get("when"))[:7] < today_ym),
        })
    cash_now = nw["spendable"] - nw["debts"]
    return {"items": items, "total": round(total, 2), "projected": round(cash_now + total, 2)}


# ── routes ───────────────────────────────────────────────────────────
@router.get("/dash", response_class=HTMLResponse)
async def dash_page(request: Request):
    if not settings.DASHBOARD_TOKEN:
        return Response("dashboard disabled", status_code=503)
    if not _authorized(request):
        return Response("unauthorized", status_code=403)
    html = (_HTML.read_text(encoding="utf-8")
        .replace("/*CATLABELS*/{}", _cat_labels_json())
        .replace("/*CATSLOTS*/{}", _cat_order_json()))
    resp = HTMLResponse(html)
    # If they arrived with ?t=…, stash it in a cookie so the token leaves the URL bar.
    if request.query_params.get("t"):
        resp.set_cookie("dash", settings.DASHBOARD_TOKEN, httponly=True,
                        samesite="lax", secure=True, max_age=60 * 60 * 24 * 30)
    return resp


@router.get("/label", response_class=HTMLResponse)
async def label_page(request: Request):
    if not settings.DASHBOARD_TOKEN:
        return Response("disabled", status_code=503)
    if not _authorized(request):
        return Response("unauthorized", status_code=403)
    resp = HTMLResponse(_LABEL_HTML.read_text(encoding="utf-8"))
    if request.query_params.get("t"):
        resp.set_cookie("dash", settings.DASHBOARD_TOKEN, httponly=True,
                        samesite="lax", secure=True, max_age=60 * 60 * 24 * 30)
    return resp


@router.get("/api/unlabeled")
async def api_unlabeled(request: Request):
    """Uncategorized spending, grouped by merchant, biggest impact first."""
    if not _authorized(request):
        return _deny()
    async with Session() as s:
        rows = (await s.execute(select(Transaction).where(
            Transaction.category.is_(None),
            Transaction.amount < 0,
            Transaction.status.not_in(("ignored", "reconciled")),
        ))).scalars().all()
        groups: dict[str, dict] = {}
        for t in rows:
            k = categories.merchant_key(t.merchant_desc)
            g = groups.setdefault(k, {"key": k, "display": t.merchant_desc, "count": 0,
                                      "total": 0.0, "sources": set(), "first": "9999", "last": "0000"})
            g["count"] += 1
            g["total"] += abs(t.amount)
            g["sources"].add(t.source)
            if len(t.merchant_desc or "") < len(g["display"] or ""):
                g["display"] = t.merchant_desc  # shortest variant reads cleanest
            d = aware(t.posted_at or t.created_at)
            if d:
                ds = d.date().isoformat()
                g["first"] = min(g["first"], ds)
                g["last"] = max(g["last"], ds)
        out = sorted(groups.values(), key=lambda g: -g["total"])
        for g in out:
            g["total"] = round(g["total"], 2)
            g["sources"] = sorted(g["sources"])
        return {"groups": out, "total": round(sum(g["total"] for g in out), 2),
                "txns": sum(g["count"] for g in out),
                "categories": _cat_options()}


@router.post("/api/label")
async def api_label(request: Request):
    """Apply Momo's labels: fix matching transactions + teach merchant memory."""
    if not _authorized(request):
        return _deny()
    body = await request.json()
    labels = body.get("labels") or {}
    if not isinstance(labels, dict):
        return JSONResponse({"error": "bad payload"}, status_code=400)
    valid = set(categories.CATEGORIES)
    async with Session() as s:
        # The bulk labeler can rewrite hundreds of rows in one submit — it was the one
        # writer with no undo at all. Same contract as every tool now: one Change row,
        # actor=dash, every touched transaction and memory snapshotted.
        async with CL.watching(s, tool="label", actor="dash",
                               args={"n_labels": len(labels)}) as rec:
            rows = (await s.execute(select(Transaction).where(
                Transaction.category.is_(None),
                Transaction.status.not_in(("ignored", "reconciled")),
            ))).scalars().all()
            touched = 0
            for t in rows:
                k = categories.merchant_key(t.merchant_desc)
                lab = labels.get(k)
                if not lab or lab.get("cat") not in valid:
                    continue
                before = CL.snapshot_row(t, ["category", "note", "status"])
                if lab["cat"] == categories.TRANSFER:
                    t.status = "ignored"
                t.category = lab["cat"]
                if lab.get("note"):
                    t.note = lab["note"]
                rec.row("transactions", t.id, before,
                        CL.snapshot_row(t, ["category", "note", "status"]))
                touched += 1
            taught = 0
            cols = ["category", "note", "is_income", "necessary"]
            for k, lab in labels.items():
                if lab.get("cat") not in valid:
                    continue
                mem = await s.get(MerchantMemory, k)
                if mem is None:
                    mem = MerchantMemory(
                        key=k, category=lab["cat"], note=lab.get("note"),
                        is_income=(False if lab["cat"] == categories.TRANSFER else None),
                        necessary=bool(lab.get("nec")),
                    )
                    s.add(mem)
                    rec.row("merchant_memory", k, None, CL.snapshot_row(mem, cols))
                    taught += 1
                else:
                    before = CL.snapshot_row(mem, cols)
                    mem.category = lab["cat"]
                    if lab.get("note"):
                        mem.note = lab["note"]
                    rec.row("merchant_memory", k, before, CL.snapshot_row(mem, cols))
            await s.commit()
            rec.says(f"批次標籤：{touched} 筆交易、{taught} 個新商家")
        return {"ok": True, "labeled_txns": touched, "merchants_taught": taught}


@router.get("/api/overview")
async def api_overview(request: Request):
    if not _authorized(request):
        return _deny()
    async with Session() as s:
        f = await F.build(s)
        nw = f.nw
        pending = await _pending_block(s, nw)
        try:
            b = await allowance.compute(s)
            b["explain"] = allowance.explain(b)
        except Exception as e:            # never blank the whole page over the budget
            print(f"[allowance] {e!r}")
            b = None
        return {
            "as_of": now().isoformat(),
            "net_worth": nw["net"],
            "assets": nw["assets"],
            "debts": nw["debts"],
            "spendable": nw["spendable"],
            "invest": nw["invest"],
            "runway_net": nw["runway_net"],
            "haircut": nw["haircut"],
            # ONE account list. There used to be a second one here ("synced", straight
            # off the Account table, un-deduplicated) and the two disagreed on screen.
            "accounts": nw["rows"],
            "pending": pending,
            "budget": b,
            "audit": f.audit(),
        }


@router.get("/api/trends")
async def api_trends(request: Request):
    if not _authorized(request):
        return _deny()
    async with Session() as s:
        snaps = (await s.execute(select(Snapshot).order_by(Snapshot.day))).scalars().all()
        net_series = [{"day": r.day, "net": round(r.net_worth, 2),
                       "assets": round(r.assets, 2), "debts": round(r.debts, 2)} for r in snaps]

        # Every series below comes from the same Facts object. Previously the monthly
        # bars summed abs(amount) on posted_at while the half-month flows used the budget
        # helpers — so a returned $1,047 Amazon order showed as spending in one chart and
        # as a credit in the other, in the same response.
        f = await F.build(s)
        keys = P.last_n(budget.current_key(), 12)   # 12 half-months = 6 months
        return {"net_worth_series": net_series,
                "category_spend": f.category_spend(90),
                "monthly": f.monthly(6),
                "flows": f.flows(keys),
                "audit": f.audit()}


@router.get("/api/income")
async def api_income(request: Request):
    if not _authorized(request):
        return _deny()
    async with Session() as s:
        # budget.is_income, not status == "income": a 劇組報帳 or a friend's Zelle is
        # money in and is not earnings. The old filter counted both.
        rows = [t for t in (await s.execute(
            select(Transaction).where(Transaction.amount > 0))).scalars().all()
            if budget.is_income(t)]
        rows = sorted(rows, key=lambda t: aware(t.posted_at or t.created_at) or now(), reverse=True)
        received, total = [], 0.0
        for t in rows:
            d = aware(t.posted_at or t.created_at)
            amt = abs(t.amount)
            total += amt
            method, note = "", (t.note or "")
            if "｜" in note:
                method, note = note.split("｜", 1)
            received.append({
                "date": d.strftime("%Y-%m-%d") if d else "",
                "name": t.merchant_desc, "method": method, "note": note, "amount": round(amt, 2),
            })
        pend = await prefs.pending_invoices(s)
        today_ym = now().strftime("%Y-%m")
        expected = [{
            "note": p.get("note") or "某案", "amount": float(p.get("amount") or 0),
            "when": p.get("when"),
            "overdue": bool(p.get("when") and str(p.get("when"))[:7] < today_ym),
        } for p in pend]
        return {
            "received": received,
            "received_total": round(total, 2),
            "expected": expected,
            "expected_total": round(sum(e["amount"] for e in expected), 2),
        }


_SOURCE_NAMES = {
    "applecard": "Apple Card（帳單）",
    "screenshot": "Apple Card（截圖）",
    "shortcut": "Apple Pay tap",
    "manual": "手動記帳",
    "notion": "歷史收入（Notion）",
}


async def _account_names(session) -> dict[str, str]:
    """account_id → human name (bank accounts get their synced names)."""
    out = dict(_SOURCE_NAMES)
    for a in (await session.execute(select(Account))).scalars().all():
        out[a.id] = a.name or a.id
    return out


@router.get("/api/ledger")
async def api_ledger(request: Request):
    """Raw transaction browser: filter by account, month, category, or search; paginated."""
    if not _authorized(request):
        return _deny()
    q = request.query_params
    f_account = q.get("account") or None
    f_month = q.get("month") or None          # 'YYYY-MM'
    f_cat = q.get("category") or None         # category name, or '未分類'
    f_text = (q.get("q") or "").strip().lower() or None
    offset = max(0, int(q.get("offset") or 0))
    limit = min(200, max(1, int(q.get("limit") or 100)))

    async with Session() as s:
        names = await _account_names(s)
        rows = (await s.execute(select(Transaction))).scalars().all()

        def eff(t):
            # budget.eff_date, not posted_at: a refund belongs to the month of the charge
            # it reverses, or the month filter here disagrees with the charts.
            d = budget.eff_date(t)
            return aware(t.effective_at or t.posted_at or t.created_at) if d else None

        # account facet key: bank rows by account_id; others by source
        def acct_key(t):
            return t.account_id if t.account_id in names and t.source == "simplefin" else (
                t.account_id if t.source == "simplefin" else t.source)

        enriched = []
        months, accounts, cats = {}, {}, {}
        for t in rows:
            d = eff(t)
            if not d:
                continue
            ak = acct_key(t)
            am = names.get(ak, ak)
            ym = d.strftime("%Y-%m")
            cat = t.category or "未分類"
            months[ym] = months.get(ym, 0) + 1
            accounts[ak] = accounts.get(ak, {"name": am, "n": 0})
            accounts[ak]["n"] += 1
            cats[cat] = cats.get(cat, 0) + 1
            enriched.append((d, ak, am, ym, cat, t))

        sel = []
        for d, ak, am, ym, cat, t in enriched:
            if f_account and ak != f_account:
                continue
            if f_month and ym != f_month:
                continue
            if f_cat and cat != f_cat:
                continue
            if f_text and f_text not in (t.merchant_desc or "").lower() and f_text not in (t.note or "").lower():
                continue
            sel.append((d, ak, am, cat, t))
        sel.sort(key=lambda x: x[0], reverse=True)

        total_out = sum(budget.spend_amount(t) for d, ak, am, cat, t in sel if budget.is_spend(t))
        total_in = sum(t.amount for d, ak, am, cat, t in sel if budget.is_income(t))
        page = sel[offset:offset + limit]
        return {
            "rows": [{
                "date": d.strftime("%Y-%m-%d"), "account": am, "merchant": t.merchant_desc,
                "amount": round(t.amount, 2), "category": cat,
                "cat_label": taxonomy.label(cat) if cat != "未分類" else cat,
                "treatment": taxonomy.treatment(cat), "status": t.status,
                "source": t.source, "note": t.note, "inflow": t.inflow_kind,
            } for d, ak, am, cat, t in page],
            "matched": len(sel), "offset": offset,
            "total_out": round(total_out, 2), "total_in": round(total_in, 2),
            "facets": {
                "accounts": [{"key": k, "name": v["name"], "n": v["n"]}
                             for k, v in sorted(accounts.items(), key=lambda x: -x[1]["n"])],
                "months": sorted(months.keys(), reverse=True),
                "categories": sorted(cats.keys(), key=lambda c: -cats[c]),
            },
        }


# ── accounts: the spine of the UI ────────────────────────────────────
def _acct_public(a: dict) -> dict:
    out = {k: a.get(k) for k in ("id", "name", "kind", "balance", "balance_src", "org",
                                 "balance_date", "n_txns", "first", "last", "stale_days", "sources")}
    out["coverage_note"] = acct.coverage_note(a)
    return out


@router.get("/api/accounts")
async def api_accounts(request: Request):
    """Every logical account with balance + record coverage. Apple Card counts once."""
    if not _authorized(request):
        return _deny()
    async with Session() as s:
        f = await F.build(s)
    # real_accounts() includes 'invest'. The old filter here was ("cash", "credit"), which
    # is why Self-Directed vanished from this card while still counting toward net worth.
    return {
        "accounts": [_acct_public(a) for a in f.real_accounts()],
        "records": [_acct_public(a) for a in f.record_accounts()],
        **f.totals(),
        "audit": f.audit(),
    }


def _reconstruct(rows, balance_now: float, kind: str, keys: list[str], first_cov, last_cov):
    """Closing balance (or amount owed) at the end of each half-month, walked backwards
    from today's balance. Points outside the record coverage return null — no fiction."""
    by_end = []
    for k in keys:
        _s, e = P.key_bounds(k)
        after = 0.0
        for t in rows:
            d = budget.eff_date(t)
            if d and d > e:
                after += t.amount
        # cash: balance_T = now − (flows after T).  credit: owed_T = now + (flows after T)
        val = (balance_now - after) if kind == "cash" else (balance_now + after)
        inside = bool(first_cov and last_cov and first_cov <= e.isoformat())
        by_end.append({"key": k, "label": P.label(k), "month_start": P.is_month_start(k),
                       "value": round(val, 2) if inside else None})
    return by_end


@router.get("/api/account")
async def api_account(request: Request):
    """One account: balance, coverage, half-month flows, balance curve, categories, ledger."""
    if not _authorized(request):
        return _deny()
    q = request.query_params
    aid = q.get("id") or ""
    n_periods = min(24, max(4, int(q.get("periods") or 12)))
    offset = max(0, int(q.get("offset") or 0))
    limit = min(200, max(1, int(q.get("limit") or 60)))
    f_text = (q.get("q") or "").strip().lower() or None
    f_cat = q.get("category") or None

    async with Session() as s:
        f = await F.build(s)
    if aid not in f.registry:
        return JSONResponse({"error": "unknown account"}, status_code=404)
    a = f.registry[aid]
    rows = f.buckets.get(aid, [])
    keys = P.last_n(budget.current_key(), n_periods)

    # Same helpers as every other chart — this used to sum abs(t.amount), so a refund
    # on this page ADDED to spending while the ledger total below it subtracted.
    series = f.flows(keys, account_id=aid)
    curve = _reconstruct(rows, a["balance"] or 0.0, a["kind"], keys, a["first"], a["last"])

    # ledger for this account, newest first, with optional search/category filter
    sel = []
    for t in rows:
        d = budget.eff_date(t)
        if not d:
            continue
        cat = t.category or "未分類"
        if f_cat and cat != f_cat:
            continue
        if f_text and f_text not in (t.merchant_desc or "").lower() and f_text not in (t.note or "").lower():
            continue
        sel.append((d, cat, t))
    sel.sort(key=lambda x: x[0], reverse=True)
    page = sel[offset:offset + limit]
    out_total = sum(budget.spend_amount(t) for d, c, t in sel if budget.is_spend(t))
    in_total = sum(t.amount for d, c, t in sel if budget.is_income(t))

    return {
        "account": _acct_public(a),
        "series": series,
        "curve": curve,
        "categories": f.category_spend(90, account_id=aid),
        "ledger": [{"date": d.isoformat(), "merchant": t.merchant_desc, "amount": round(t.amount, 2),
                    "category": c, "status": t.status, "source": t.source, "note": t.note}
                   for d, c, t in page],
        "matched": len(sel), "offset": offset,
        "total_out": round(out_total, 2), "total_in": round(in_total, 2),
        "facet_categories": sorted({c for _d, c, _t in sel}),
    }


@router.get("/api/brain")
async def api_brain(request: Request):
    if not _authorized(request):
        return _deny()
    async with Session() as s:
        f = await F.build(s)
        nw = f.nw
        prof = await prefs.get_income_profile(s)
        pr = await prefs.get_prefs(s)
        try:
            b = await budget.status(s)
            ib = await budget.income_basis(s)
            expected, actual = ib["expected"], ib["actual"]
        except Exception:
            b, expected, actual = None, 0.0, 0.0

        pend = await prefs.pending_invoices(s)
        booked = [{"note": p.get("note"), "amount": float(p.get("amount") or 0),
                   "when": p.get("when")} for p in pend]

        budget_detail = None
        if b:
            budget_detail = {
                "monthly_baseline": prof["monthly_baseline"],
                "booked_pipeline": booked,
                "expected_period": round(expected, 2),
                "actual_period": round(actual, 2),
                "blend": b.get("income_blend"),
                "income_period": b["income_period"],
                "fixed_monthly": pr["fixed_monthly"],
                "fixed_period": b["fixed_period"],
                "savings_amount": pr["savings_amount"],
                "savings_cadence": pr["savings_cadence"],
                "savings_period": b["savings_period"],
                "allowance": b["allowance"],
                "spent": b["spent"],
                "remaining": b["remaining"],
                "period_start": str(b["period_start"]),
                "period_end": str(b["period_end"]),
                "days_left": b["days_left"],
            }

        mem = (await s.execute(select(MerchantMemory))).scalars().all()
        memory = [{
            "key": m.key, "category": m.category, "note": m.note,
            "is_income": m.is_income, "necessary": m.necessary,
            "updated_at": aware(m.updated_at).isoformat() if m.updated_at else None,
        } for m in mem]

        txns = (await s.execute(select(Transaction))).scalars().all()
        def keyfn(t):
            d = aware(t.posted_at or t.created_at)
            return d or now()
        txns = sorted(txns, key=keyfn, reverse=True)[:400]
        ledger = [{
            "date": (aware(t.posted_at or t.created_at).strftime("%Y-%m-%d")
                     if (t.posted_at or t.created_at) else ""),
            "merchant": t.merchant_desc, "amount": round(t.amount, 2),
            "category": t.category, "status": t.status, "source": t.source,
            "note": t.note,
        } for t in txns]

        msgs = (await s.execute(
            select(Message).order_by(Message.created_at.desc(), Message.id.desc()).limit(120)
        )).scalars().all()
        chat = [{
            "role": m.role, "content": m.content,
            "at": aware(m.created_at).isoformat() if m.created_at else None,
        } for m in reversed(msgs)]

        return {
            "networth": nw,
            "budget_detail": budget_detail,
            "config": {
                "fixed_monthly": pr["fixed_monthly"],
                "savings_amount": pr["savings_amount"],
                "savings_cadence": pr["savings_cadence"],
                "monthly_baseline": prof["monthly_baseline"],
                "ytd_income": prof["ytd_income"],
                "cash_on_hand": prof["cash_on_hand"],
                "emergency_target": prof["emergency_target"],
                "total_debt": prof["total_debt"],
                "accounts": prof["accounts"],
            },
            "merchant_memory": memory,
            "ledger": ledger,
            "chat": chat,
        }


# ── Phase B: the allowance, shown with its own reasoning ─────────────
@router.get("/api/allowance")
async def api_allowance(request: Request):
    """The number, the three lenses behind it, and the sentences she'd say out loud."""
    if not _authorized(request):
        return _deny()
    async with Session() as s:
        a = await allowance.compute(s, request.query_params.get("key") or None)
        a["explain"] = allowance.explain(a)
        a["fixed_rows"] = await FX.rows(s)
        a["sinking_rows"] = await FX.sinking_rows(s)
        a["renewals"] = await FX.renewals(s, within_days=120)
        a["tax_payments_found"] = await TAX.find_prior_payments(s)
        a["income_by_month"] = await STAB.income_by_month(s)
        return a


@router.get("/settle", response_class=HTMLResponse)
async def settle_page(request: Request):
    if not _authorized(request):
        return _deny()
    return HTMLResponse(_SETTLE_HTML.read_text(encoding="utf-8"))


@router.get("/api/settle")
async def api_settle_get(request: Request):
    """Everything the settlement page needs: which period, how it went, her own last
    words, and whether the backup is stale."""
    if not _authorized(request):
        return _deny()
    from . import settle as ST
    async with Session() as s:
        st = await ST.state(s)
        # The quarter is only offered once every session inside it is settled — closing
        # a season on top of unfinished periods would allocate money those sessions have
        # not yet decided about.
        q = None if st["awaiting"] else await ST.quarter_pending(s)
        if q is not None and not request.query_params.get("key"):
            sp = await ST.spare(s)
            try:
                te = await AN.to_earn(s, 3)
                board = await SE.progress(s, te["tiers"])
            except Exception:
                board = None
            return {
                "awaiting": True, "kind": "quarter", "key": q["key"],
                "label": f"{q['start']} ~ {q['end']}",
                "quarter": q, "board": board,
                "spare": {k: sp[k] for k in ("pot", "water", "total")},
                "proposal": ST.propose(sp["pot"], sp["jars"]),
                "objective": await ST.objective(s),
                "funds": await ST.fund_options(s),
                "scored": await ST.last_quarter_objectives(s),
                # same three questions, same ids (so the answers stay a comparable
                # series) — only the unit word changes at a season boundary
                "questions": [{"id": k,
                               "q": qq.replace("這一期", "這一季").replace("下一期", "下一季")}
                              for k, qq in ST.REFLECTION],
                "noticed": None,
                "last": await ST.last_reflection(s),
                "backup": await ST.backup_state(s),
                "jars": (await allowance.compute(s))["spoken_for"]["jars"],
                "more": [],
            }
        key = request.query_params.get("key") or st.get("oldest")
        if not key:
            return {"awaiting": False, "backup": await ST.backup_state(s)}
        clo = await allowance.closure(s, key)
        return {
            "awaiting": True, "key": key, "label": P.label(key),
            "closure": clo,
            "questions": [{"id": k, "q": q} for k, q in ST.REFLECTION],
            "noticed": await ST.noticed_question(s, key),
            "last": await ST.last_reflection(s, before_key=key),
            "backup": await ST.backup_state(s),
            "jars": (await allowance.compute(s))["spoken_for"]["jars"],
            "more": st["periods"][1:],
        }


@router.post("/api/settle")
async def api_settle_post(request: Request):
    """Submit one period's settlement. Idempotent on the period key."""
    if not _authorized(request):
        return _deny()
    from . import jars as J
    from . import settle as ST
    body = await request.json()
    key = (body.get("key") or "").strip()
    dest = (body.get("destination") or "carry").strip()
    jar_id = (body.get("jar") or "").strip()
    reflection = body.get("reflection") or {}
    async with Session() as s:
        if await ST.get_one(s, key) is not None:
            return {"ok": False, "error": "這一期已經結算過了。"}
        if (body.get("kind") or "session") == "quarter":
            # The season pot empties INTO the jars Momo picked. Anything she leaves
            # unallocated simply stays where it is, and is recorded as left.
            allocs = {k: float(v or 0) for k, v in (body.get("allocations") or {}).items()
                      if float(v or 0) > 0}
            receipts = []
            async with CL.watching(s, tool="settle_quarter", args={"key": key},
                                   source_text=f"{key} 季結算", actor="settle"):
                total = round(sum(allocs.values()), 2)
                if total > 0:
                    pot = await _season_balance(s)
                    drawn = await J.draw(s, "season", min(total, pot))
                    if drawn.get("ok"):
                        receipts.append(drawn["receipt"])
                for jid, amt in allocs.items():
                    out = await J.allocate(s, jid, amt)
                    if out.get("ok"):
                        receipts.append(out["receipt"])
                await ST.record(s, key, pocket=total, destination="allocated",
                                reflection=reflection, kind="quarter",
                                allocations=allocs,
                                objective=(body.get("objectives")
                                           or ([body["objective"]] if body.get("objective") else [])))
            return {"ok": True, "moved": "；".join(receipts) or None, "still_awaiting": []}

        clo = await allowance.closure(s, key)
        pocket = float(clo.get("pool") or 0.0)
        moved = None
        async with CL.watching(s, tool="settle", args={"key": key, "destination": dest},
                                      source_text=f"{P.label(key)} 結算", actor="settle"):
            if pocket > 0 and dest == "jar" and jar_id:
                out = await J.allocate(s, jar_id, pocket)
                moved = out.get("receipt") if out.get("ok") else None
            elif pocket > 0 and dest == "quarter":
                out = await J.allocate(s, "season", pocket)
                moved = out.get("receipt") if out.get("ok") else None
            await ST.record(s, key, pocket=pocket, destination=dest,
                            reflection=reflection, kind=body.get("kind") or "session")
        st = await ST.state(s)
        return {"ok": True, "moved": moved, "still_awaiting": st["periods"]}


@router.get("/api/jars")
async def api_jars(request: Request):
    """有主的錢 — the pots, the one spoken-for total, and whether cash still covers it.
    Derived from allowance.compute() so this can never disagree with the engine."""
    if not _authorized(request):
        return _deny()
    async with Session() as s:
        a = await allowance.compute(s)
        from . import jars as J
        return {"seeded": await J.seeded(s),
                "jars": a["spoken_for"]["jars"],
                "total": a["spoken_for"]["total"],
                "reserve": a["spoken_for"]["reserve"],
                "available": a["available"],
                "breach": a["jar_breach"]}


@router.get("/api/export")
async def api_export(request: Request):
    """The whole internal state as one JSON file, secrets stripped.

    Momo downloads this and hands it over when something on screen looks wrong — it's the
    difference between fixing what she reports and fixing what actually happened. Add
    ?txns=0 for a small file when only the config and balances matter."""
    if not _authorized(request):
        return _deny()
    want_txns = (request.query_params.get("txns") or "1") != "0"
    async with Session() as s:
        data = await EX.build(s, include_txns=want_txns)
        # so 「上次備份 X 天前」 is measured, not guessed — the reminder needs a real number
        from . import settle as ST
        await ST.stamp_export(s)
    stamp = now().strftime("%Y%m%d-%H%M")
    return JSONResponse(data, headers={
        "Content-Disposition": f'attachment; filename="chen-state-{stamp}.json"'})


# ── the detail pages ─────────────────────────────────────────────────

async def _diagnostics(s) -> list[str]:
    """The detectors that were already computing the right answer into nothing.

    Every one of these would have caught a bug the external review had to find by hand:
    fixed.reconcile's 10% alarm would have caught the $3,778-vs-$1,521 divergence, the
    unlabeled count would have caught $893 of spending that was both nagging her and
    invisible to the budget, and job-run tracking makes a silently dead alert loop —
    the thing that already happened once — visible within a day."""
    out = []
    try:
        rc = await FX.reconcile(s)
        if rc.get("diverged"):
            out.append(f"固定成本表說 {rc['stated']:.0f}／月，最近實刷 {rc['observed']:.0f}"
                       "——差超過一成，可能有一筆沒記到或漲價了")
    except Exception:
        pass
    try:
        rows = (await s.execute(select(Transaction))).scalars().all()
        unl = [t for t in rows if t.amount < 0 and t.category is None
               and t.status not in ("ignored", "reconciled")]
        if len(unl) >= 5:
            total = sum(-t.amount for t in unl)
            out.append(f"有 {len(unl)} 筆共 ${total:,.0f} 還沒分類（現在照樣吃額度）"
                       f"——去 /label 清一清")
    except Exception:
        pass
    try:
        for job, days, name in (("alerts", 1.5, "超支提醒"), ("weekly_report", 9, "週報")):
            raw = await get_kv(s, f"last_run:{job}")
            if raw:
                ago = (now() - datetime.fromisoformat(raw)).total_seconds() / 86400
                if ago > days:
                    out.append(f"{name}已經 {ago:.0f} 天沒跑了——排程可能死了")
    except Exception:
        pass
    return out



async def _season_board(s):
    try:
        te = await AN.to_earn(s, 3)
        sb = await SE.progress(s, te["tiers"])
        return sb
    except Exception:
        return None


@router.get("/api/plan")
async def api_plan(request: Request):
    """計畫: where the money goes, how that has trended, where the walls are, and how
    much has to land in the next three months."""
    if not _authorized(request):
        return _deny()
    async with Session() as s:
        f = await F.build(s)
        te = await AN.to_earn(s, 3, f)
        rec = te.get("fixed_reconcile") or {}
        return {
            "categories": await AN.category_series(s, 12, f),
            "standing": await AN.standing(s, f),
            "to_earn": te,
            # Two modules, because they answer two different questions on two different
            # clocks: what this season DID (cash, mostly already decided) and what she has
            # to book now (which lands next season). See season.settlement / AN.to_book.
            # the scoreboard: frozen targets, the dated event log, 這季自己談下來的 —
            # fully built since the season work, never wired to a surface until now
            "season_board": await _season_board(s),
            "settlement": await SE.settlement(
                s, f, burn_monthly=te["fixed_monthly"] + te["normal_flex_monthly"],
                by_hand_monthly=rec.get("by_hand_monthly", 0.0),
                by_hand_rows=rec.get("by_hand_rows")),
            "to_book": await AN.to_book(s, f),
            # Momo's three layers: the fortnight (what I can spend and what kind of tight
            # this is), the runway (the earning goal, as a schedule with deadlines), and
            # the season settlement above.
            "fortnight": await AN.fortnight(s, f),
            "runway": await RW.plan(s, f),
            "audit": f.audit() + await _diagnostics(s),
        }


@router.get("/api/projects")
async def api_projects(request: Request):
    """專案: one record per job — Momo's idea, and the fix for three lists of the same thing.

    "since so much of my earning is related to project, we should just start a tab that
    tracks project… It's a good record to have." The invoice archive, the pending list and
    the ledger each held a third of every gig; this lines them up on the amount."""
    if not _authorized(request):
        return _deny()
    async with Session() as s:
        return await PJ.summary(s)


@router.get("/api/calendar")
async def api_calendar(request: Request):
    if not _authorized(request):
        return _deny()
    days = min(800, max(30, int(request.query_params.get("days") or 400)))
    async with Session() as s:
        return await AN.calendar_items(s, days)


@router.get("/api/changes")
async def api_changes(request: Request):
    """異動紀錄: everything 陳會計 has changed, newest first.

    Momo gave her full authority to write on the condition that every write reports
    itself. This is the other half of that deal — the reporting has to survive the
    message scrolling away."""
    if not _authorized(request):
        return _deny()
    limit = min(300, max(10, int(request.query_params.get("limit") or 80)))
    async with Session() as s:
        return {"changes": await CL.recent(s, limit)}


@router.post("/api/maintenance")
async def api_maintenance(request: Request):
    """The old boot-time seed chain, now run on purpose. See main.run_maintenance."""
    if not _authorized(request):
        return _deny()
    from . import main as M
    log = await M.run_maintenance()
    return {"ok": True, "log": log or ["每個 pass 都跑過了，沒有東西要重掃。"]}


@router.post("/api/undo")
async def api_undo(request: Request):
    """Put one change back. The old value came out of the log, not out of a guess."""
    if not _authorized(request):
        return _deny()
    try:
        body = await request.json()
        cid = int(body.get("id"))
    except (ValueError, TypeError, AttributeError):
        return JSONResponse({"ok": False, "error": "沒有指定要還原哪一筆"}, status_code=400)
    async with Session() as s:
        res = await CL.undo(s, cid)
        if res.get("ok"):
            await write_snapshot(s)     # net worth may have moved; keep the curve honest
        return JSONResponse(res, status_code=200 if res.get("ok") else 409)


@router.get("/api/income2")
async def api_income2(request: Request):
    """收入: performance by half-month / month / year, the payer mix, and the index."""
    if not _authorized(request):
        return _deny()
    async with Session() as s:
        f = await F.build(s)
        perf = await AN.income_performance(s, f)
        perf["to_earn"] = await AN.to_earn(s, 3, f)
        perf["projection"] = await AN.projection(s, 3, f)
        perf["pending"] = await _pending_block(s, f.nw)
        perf["networth"] = f.nw
        perf["audit"] = f.audit()
        return perf


async def _season_balance(s) -> float:
    from . import jars as J
    js = await J.load(s)
    return float((J.get(js, "season") or {}).get("balance") or 0.0)

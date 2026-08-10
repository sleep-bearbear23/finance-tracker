"""One-time history import from Momo's old Notion (帳務 2025 + 2026 收入匯款記錄).

Momo is retiring Notion, so these are baked in as a static seed and inserted once on boot
(idempotent — safe to run every deploy). Net-worth points give the dashboard a real trend
from Sep 2025; income rows give the 收入 tab an actual record for 2026.
"""
from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy import select

from . import categories, prefs
from .config import TZ
from .db import get_kv, set_kv
from .models import MerchantMemory, Snapshot, Transaction

# Momo's known work payers, lifted from the 2026 income record — money from these is real pay.
# Everything else that comes in (friends' Zelle bill-splits, transfers) is NOT assumed to be income.
INCOME_SOURCES = [
    "Jump Deer Media", "Yue Zhou", "Checkout Productions", "Ever After Production",
    "Bad Larry", "Anastasia Wibisono", "Horizon Vista", "Exact Film",
]

# (day, net_worth, assets, debts) — computed from the Notion 帳務 2025 per-account snapshots.
SNAP_HISTORY = [
    ("2025-09-25", 17259.56, 24416.77, 7157.21),
    ("2025-09-29", 18059.28, 25421.18, 7361.90),
    ("2025-10-01", 18661.98, 25351.86, 6689.88),
    ("2025-10-04", 18000.26, 25355.15, 7354.89),
    ("2025-10-07", 15696.03, 23608.34, 7912.31),
    ("2025-10-12", 15730.05, 24043.84, 8313.79),
    ("2025-10-13", 14785.35, 23036.23, 8250.88),
    ("2025-10-17", 13621.35, 22038.89, 8417.54),
    ("2025-10-19", 13575.77, 22039.67, 8463.90),
    ("2025-10-22", 13503.58, 22022.81, 8519.23),
    ("2025-10-23", 13672.95, 22222.10, 8549.15),
    ("2025-10-31", 16502.72, 25701.25, 9198.53),
    ("2025-11-12", 14324.20, 19482.61, 5158.41),
    ("2025-12-04", 13094.50, 18930.59, 5836.09),
    ("2026-01-21", 11905.88, 16706.66, 4800.78),
    ("2026-03-12", 15303.87, 20347.21, 5043.34),
    ("2026-05-24", 15026.49, 20576.22, 7849.73),
    ("2026-07-02", 13103.55, 17323.80, 2420.25),
]

# (date, invoice/project, amount, method, bank record) — from 2026 收入匯款記錄.
INCOME_HISTORY = [
    ("2026-01-12", "EFO_Day Rate Payroll", 300.00, "Zelle", "Zelle from YUE ZHOU"),
    ("2026-01-20", "CKO_Principal Photography Payroll", 500.00, "Zelle", "Zelle from CHECKOUT PRODUCTIONS LLC"),
    ("2026-02-02", "TUS_Payroll", 2700.00, "Physical Check", "ONLINE DEPOSIT"),
    ("2026-02-04", "TCT_Day Rate Payroll", 500.00, "Zelle", "Zelle from YUE ZHOU"),
    ("2026-02-24", "LBVA_commissionRate", 550.00, "Zelle", "Zelle from EVER AFTER PRODUCTION LLC"),
    ("2026-02-25", "LRME_Day Rate Payroll", 2156.42, "Account Transfer", "Bad Larry Produc"),
    ("2026-03-06", "MLU_Flat Pay", 450.00, "Zelle", "Zelle from ANASTASIA WIBISONO"),
    ("2026-03-27", "EHG_Payroll", 900.00, "Zelle", "Zelle from HORIZON VISTA PRODUCTION"),
    ("2026-03-30", "AVG.MAR26_Payroll", 800.00, "Zelle", "Zelle from JUMP DEER MEDIA, INC."),
    ("2026-04-14", "Pegasus III Payroll", 300.00, "Zelle", "Zelle from YUE ZHOU"),
    ("2026-04-23", "AVG Payroll APRIL", 1000.00, "Zelle", "Zelle from JUMP DEER MEDIA, INC."),
    ("2026-04-28", "HMH Payroll", 1400.00, "Account Transfer", "EXACT FILM LLC PAYROLL"),
    ("2026-05-20", "AVG MAY Payroll", 400.00, "Zelle", "Zelle from JUMP DEER MEDIA, INC."),
]


def _noon(day: str) -> datetime:
    return datetime.strptime(day, "%Y-%m-%d").replace(hour=12, tzinfo=TZ)


async def backfill(session) -> int:
    """Insert historical snapshots + income, skipping anything already present. Returns rows added."""
    added = 0

    have_days = set((await session.execute(select(Snapshot.day))).scalars().all())
    for day, net, assets, debts in SNAP_HISTORY:
        if day in have_days:
            continue
        session.add(Snapshot(day=day, net_worth=net, assets=assets, debts=debts, cash=assets,
                             allowance=0.0, spent=0.0, income_biweekly=0.0, created_at=_noon(day)))
        added += 1

    for i, (day, invoice, amt, method, rec) in enumerate(INCOME_HISTORY):
        tid = f"notion:income:{day}:{i}"
        if await session.get(Transaction, tid):
            continue
        session.add(Transaction(
            id=tid, account_id="notion", amount=abs(amt), merchant_desc=invoice,
            category="Income", note=f"{method}｜{rec}", status="income", source="notion",
            posted_at=_noon(day), created_at=_noon(day),
        ))
        added += 1

    # Seed the known work payers (merge, so Momo's later additions survive).
    srcs = prefs._load_list(await get_kv(session, "cfg_income_sources"))
    have = {prefs._norm(x) for x in srcs}
    for name in INCOME_SOURCES:
        if prefs._norm(name) not in have:
            srcs.append(name)
    await set_kv(session, "cfg_income_sources", json.dumps(srcs))

    if added:
        await session.commit()
    return added


async def reclassify_income(session) -> int:
    """One-time: money that was auto-marked income but isn't from a known work payer (and wasn't
    confirmed by Momo) gets pulled back out of income. Runs once, guarded by a KV flag."""
    if await get_kv(session, "income_cleanup_v1") == "1":
        return 0
    rows = (await session.execute(
        select(Transaction).where(
            Transaction.amount > 0,
            Transaction.status == "income",
            Transaction.source.in_(("simplefin", "screenshot")),
        )
    )).scalars().all()
    moved = 0
    for t in rows:
        if await prefs.is_work_income_source(session, t.merchant_desc):
            continue  # from a known work payer — keep as income
        mem = await session.get(MerchantMemory, categories.merchant_key(t.merchant_desc))
        if mem is not None and mem.is_income is True:
            continue  # Momo already confirmed this sender is income — keep
        t.status = "ignored"
        t.category = "Transfers/Ignore"
        t.note = (t.note or "") + "（自動：非工作收入，已從收入移除）"
        moved += 1
    await set_kv(session, "income_cleanup_v1", "1")
    if moved:
        await session.commit()
    return moved

"""Phase A verification — run the real 1,727 Apple Card rows through the new pipeline
and check the buckets against the figures worked out by hand with Momo.

    PYTHONPATH=. python3 tests_phase_a.py
"""
from __future__ import annotations

import asyncio
import json
import os
import statistics
from collections import defaultdict
from datetime import datetime
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("ANTHROPIC_API_KEY", "x")
os.environ.setdefault("LINE_CHANNEL_ACCESS_TOKEN", "x")
os.environ.setdefault("LINE_CHANNEL_SECRET", "x")

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app import budget, migrate, retag  # noqa: E402
from app import taxonomy as T  # noqa: E402
from app.config import TZ  # noqa: E402
from app.models import Base, Transaction  # noqa: E402

DATA = Path(__file__).parent / "app" / "data" / "applecard.json"

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{('  — ' + detail) if detail else ''}")


async def load(session, rows):
    """Insert the raw statement rows the way the old importer did: legacy category names."""
    for r in rows:
        session.add(Transaction(
            id=r["id"], account_id="applecard", amount=r["amount"],
            merchant_desc=r["desc"],
            posted_at=datetime.fromisoformat(r["date"]).replace(tzinfo=TZ),
            category=r.get("cat"),          # <- OLD English names, as in production
            status="auto", source="applecard",
        ))
    await session.commit()


async def main():
    raw = json.loads(DATA.read_text())
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as c:
        await c.run_sync(Base.metadata.create_all)
    await migrate.run(engine)  # must be a no-op on a fresh schema
    Session = async_sessionmaker(engine, expire_on_commit=False)

    async with Session() as s:
        await load(s, raw)

        print("\n[1] retag — every row lands on a taxonomy id or honestly on None")
        stats = await retag.retag(s)
        rows = (await s.execute(__import__("sqlalchemy").select(Transaction))).scalars().all()
        bad = [t for t in rows if t.category is not None and t.category not in T.ALL]
        check("no legacy category names survive", not bad,
              f"{len(bad)} left, e.g. {bad[0].category if bad else ''}")
        unknown = [t for t in rows if t.category is None]
        pct = 100 * (len(rows) - len(unknown)) / len(rows)
        check("coverage >= 93%", pct >= 93, f"{pct:.1f}% ({len(unknown)} unlabeled)")
        check("retag is idempotent", (await retag.retag(s)) == {})

        print("\n[2] treatments — every category maps to exactly one tag")
        check("all ids tagged", all(T.treatment(c) for c in T.ALL))
        check("allowance = flex + want",
              set(c for c in T.ALL if T.in_allowance(c))
              == set(T.of_treatment(T.FLEX)) | set(T.of_treatment(T.WANT)))
        check("fixed is not discretionary", not T.in_allowance("rent"))
        check("work is not discretionary", not T.in_allowance("work"))
        check("irregular is not discretionary", not T.in_allowance("travel"))

        print("\n[3] refund netting — 41% of Amazon comes back")
        nets = await retag.net_refunds(s)
        total = sum(nets.values())
        print(f"      {nets}")
        check("most credits are recognised as returns",
              (nets["matched"] + nets["by_marker"]) >= 0.8 * total,
              f"{nets['matched'] + nets['by_marker']}/{total}")
        refunds = [t for t in rows if t.amount > 0 and t.nets_txn_id]
        check("matched refunds inherit a category",
              all(r.category is not None for r in refunds))
        check("no refund points at a credit",
              all(next(c for c in rows if c.id == r.nets_txn_id).amount < 0 for r in refunds))

        print("\n[3b] a refund is counted in the month of the CHARGE, not the credit")
        moved = [t for t in rows if t.effective_at and t.posted_at
                 and t.effective_at.date() != t.posted_at.date()]
        check("some refunds were re-dated", len(moved) > 0, f"{len(moved)} re-dated")
        check("re-dating always moves earlier",
              all(t.effective_at <= t.posted_at for t in moved))
        by_m = defaultdict(float)
        for t in rows:
            if not budget.is_discretionary(t):
                continue
            d = budget.eff_date(t)
            if d and "2026-02" <= d.isoformat()[:7] <= "2026-07":
                by_m[d.isoformat()[:7]] += budget.spend_amount(t)
        check("no month has negative discretionary spend",
              all(v >= 0 for v in by_m.values()),
              str({k: round(v) for k, v in sorted(by_m.items())}))

        print("\n[4] budget maths — refunds subtract, fixed/work/irregular stay out")
        gross = sum(-t.amount for t in rows if t.amount < 0)
        net = sum(budget.spend_amount(t) for t in rows if budget.is_spend(t))
        check("net spend < gross charges", net < gross, f"net {net:,.0f} vs gross {gross:,.0f}")
        disc = sum(budget.spend_amount(t) for t in rows if budget.is_discretionary(t))
        check("discretionary < all spend", disc < net, f"disc {disc:,.0f} of {net:,.0f}")
        check("a refund is spend but negative",
              any(budget.is_spend(t) and budget.spend_amount(t) < 0 for t in rows))
        check("no transfer/tax row counts as spend",
              not any(budget.is_spend(t) for t in rows if T.is_skip(t.category)))
        check("a refund is never income", not any(budget.is_income(t) for t in refunds))

        print("\n[5] the numbers we told Momo")
        per_tr = defaultdict(lambda: defaultdict(float))
        for t in rows:
            if not budget.is_spend(t):
                continue
            d = budget.eff_date(t)
            if not d or not ("2026-02" <= d.isoformat()[:7] <= "2026-07"):
                continue
            per_tr[T.treatment(t.category)][d.isoformat()[:7]] += budget.spend_amount(t)
        months = [f"2026-{m:02d}" for m in range(2, 8)]
        for tag in (T.FLEX, T.WANT, T.FIXED, T.WORK, T.IRREGULAR):
            vals = [per_tr[tag].get(m, 0.0) for m in months]
            print(f"      {T.TREATMENT_LABEL[tag]:6} median {statistics.median(vals):8.2f}"
                  f"  mean {sum(vals)/6:8.2f}   {[round(v) for v in vals]}")
        flex = [per_tr[T.FLEX].get(m, 0.0) for m in months]
        check("彈性 median near the $1,092 we quoted",
              900 <= statistics.median(flex) <= 1300, f"{statistics.median(flex):.2f}")
        allw = [per_tr[T.FLEX].get(m, 0.0) + per_tr[T.WANT].get(m, 0.0) for m in months]
        check("allowance-eating median near the $1,235 we quoted",
              1000 <= statistics.median(allw) <= 1500, f"{statistics.median(allw):.2f}")

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILED: " + ", ".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

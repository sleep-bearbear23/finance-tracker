"""The 2026-08-15 external review, executed — each block is one accepted finding.

An outside session read the codebase cold and measured it against Momo's production
export. The pattern it named: two layers of truth, hand-entered and measured, and the
stale one wins silently. These tests pin the fixes so the pattern cannot quietly
reassemble itself.

    PYTHONPATH=. python3 tests_review.py
"""
from __future__ import annotations

import asyncio
import os
from datetime import date, timedelta
from pathlib import Path

os.environ.update(
    DATABASE_URL="sqlite+aiosqlite:////tmp/review.db",
    ANTHROPIC_API_KEY="x", LINE_CHANNEL_ACCESS_TOKEN="x", LINE_CHANNEL_SECRET="x",
    DASHBOARD_TOKEN="tok",
)
Path("/tmp/review.db").unlink(missing_ok=True)

from sqlalchemy import select  # noqa: E402

from app import allowance, budget, changelog, claims, fixed, migrate, retag, simplefin, tax, tools  # noqa: E402
from app import taxonomy as T  # noqa: E402
from app.config import TZ, now  # noqa: E402
from app.db import Session, engine, get_kv, init_db, set_kv  # noqa: E402
from app.models import Change, MerchantMemory, Transaction  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{('  — ' + detail) if detail else ''}")


def near(a, b, eps=0.011):
    return abs((a or 0) - (b or 0)) < eps


async def main():
    await init_db()
    await migrate.run(engine)
    today = now()

    async with Session() as s:
        print("\n[1] A-2 — a pending row is not allowed to stay undated forever")
        # the bank sends a pending charge: no posted date
        await simplefin.absorb(s, "chase", {"id": "tx-pend", "amount": "-20.00",
                                            "description": "OPENAI *CHATGPT SUBSCR"}, True)
        await s.commit()
        t = await s.get(Transaction, "tx-pend")
        check("it lands, undated — the bank genuinely has not said when",
              t is not None and t.posted_at is None, str(t.posted_at if t else None))
        check("…but it still belongs to a period, through created_at",
              budget.eff_date(t) is not None, str(budget.eff_date(t)))
        # next poll: same id, now settled
        stamp = int((today - timedelta(days=1)).timestamp())
        await simplefin.absorb(s, "chase", {"id": "tx-pend", "amount": "-20.00",
                                            "description": "OPENAI *CHATGPT SUBSCR",
                                            "posted": stamp}, True)
        await s.commit()
        t = await s.get(Transaction, "tx-pend")
        check("when the bank posts the date, the row gets it", t.posted_at is not None,
              str(t.posted_at))

        print("\n[2] A-2 — a charge that settles under a NEW id is the same money once")
        await simplefin.absorb(s, "chase", {"id": "tx-old", "amount": "1400.00",
                                            "description": "ZELLE PAYMENT FROM JUMP DEER MEDIA, IN"}, True)
        await s.commit()
        old = await s.get(Transaction, "tx-old")
        old.status, old.category, old.note = "income", None, "八月那筆"  # what she taught it
        old.inflow_kind = T.PAY
        await s.commit()
        await simplefin.absorb(s, "chase", {"id": "tx-new", "amount": "1400.00",
                                            "description": "ZELLE PAYMENT FROM JUMP DEER MEDIA, IN",
                                            "posted": stamp}, True)
        await s.commit()
        gone = await s.get(Transaction, "tx-old")
        kept = await s.get(Transaction, "tx-new")
        check("the undated twin is absorbed, not duplicated", gone is None and kept is not None)
        check("…dated, and still carrying everything she taught it",
              kept.posted_at is not None and kept.status == "income"
              and kept.note == "八月那筆" and kept.inflow_kind == T.PAY,
              f"{kept.status}/{kept.note}")
        both = [x for x in (await s.execute(select(Transaction))).scalars()
                if near(x.amount, 1400)]
        check("exactly one $1,400 in the ledger", len(both) == 1, str(len(both)))

        print("\n[3] A-3 / F-6 — one engine, and the typed fixed-cost figure is dead")
        await set_kv(s, "cfg_fixed_monthly", "3778.38")   # the stale onboarding layer
        await allowance.set_start(s, today.date().isoformat()) if hasattr(allowance, "set_start") else None
        b = await budget.status(s)
        a = await allowance.compute(s)
        check("the phone's number IS the dashboard's number",
              near(b["allowance"], a["allowance"]) and near(b["spent"], a["spent"]),
              f'{b["allowance"]} vs {a["allowance"]}')
        rows_total = await fixed.monthly_total(s)
        check("fixed costs come from the itemized rows, not the $3,778 KV",
              near(b["fixed_monthly"], rows_total) and not near(b["fixed_monthly"], 3778.38),
              f'{b["fixed_monthly"]}')
        from app import prefs as PR
        pr = await PR.get_prefs(s)
        check("get_prefs agrees — the KV is unread everywhere",
              near(pr["fixed_monthly"], rows_total), str(pr["fixed_monthly"]))
        check("a surface can tell 額度 0 from 沒設定", "configured" in b)

        print("\n[4] F-4 — the $1,836 IRS payment counts as paid")
        s.add(Transaction(id="tx-irs", account_id="chase", amount=-1836.0,
                          merchant_desc="IRS USATAXPYMT 2226 WEB ID: 3387702000",
                          posted_at=now().replace(month=6, day=15), status="auto"))
        s.add(Transaction(id="tx-pay1", account_id="chase", amount=2000.0,
                          merchant_desc="ZELLE FROM CLIENT", status="income",
                          inflow_kind=T.PAY, posted_at=now().replace(month=6, day=1)))
        await s.commit()
        st = await tax.status(s)
        check("already_paid sees the uncategorised IRS row",
              near(st["already_paid"], 1836.0), str(st["already_paid"]))
        check("…and outstanding is reduced by it, not asking her to hold it twice",
              st["outstanding"] < st["should_hold"] or st["should_hold"] == 0,
              f'{st["outstanding"]} of {st["should_hold"]}')

        print("\n[5] the phantom June rent — a fixed cost has a birthday")
        jun = await fixed.monthly_total(s, on=date(2026, 6, 15))
        jul = await fixed.monthly_total(s, on=date(2026, 7, 15))
        check("June does not include rent (since 2026-07-01)",
              near(jul - jun, 1000.0), f"jun {jun} jul {jul}")
        pp_jun = await fixed.per_period(s, "2026-06A")
        pp_jul = await fixed.per_period(s, "2026-07A")
        check("…and the per-period split honours it", pp_jul > pp_jun + 400,
              f"{pp_jun} vs {pp_jul}")

        print("\n[6] B-4 — the system may not eat her answers")
        # merchant memory: a failed lookup keeps what she taught
        await set_kv(s, retag.RETAG_FLAG, "")
        m = MerchantMemory(key="ranchmarket", category="SomeLegacyName")
        s.add(m)
        await s.commit()
        await retag.retag(s)
        m = await s.get(MerchantMemory, "ranchmarket")
        check("a memory that maps to nothing keeps its old value, never blanked",
              m.category is not None, str(m.category))

        # family payback: her recategorisation survives a force re-sweep
        s.add(Transaction(id="tx-mom", account_id="chase", amount=200.0,
                          merchant_desc="Online Transfer From CHK ...7567",
                          category="gifts", inflow_kind=T.REIMBURSE_FAMILY,
                          status="enriched", posted_at=now()))
        await s.commit()
        await retag.net_family_paybacks(s, force=True)
        t = await s.get(Transaction, "tx-mom")
        check("a payback she recategorised is NOT forced back to household",
              t.category == "gifts" and t.status == "enriched", f"{t.category}/{t.status}")

        # refund netting: an enriched credit is hers
        s.add(Transaction(id="tx-chg", account_id="apple", amount=-50.0,
                          merchant_desc="AMAZON MKTPLACE PMTS", category="shopping",
                          status="auto", posted_at=now() - timedelta(days=5)))
        s.add(Transaction(id="tx-cr", account_id="apple", amount=50.0,
                          merchant_desc="AMAZON MKTPLACE PMTS", category="want",
                          status="enriched", posted_at=now()))
        await s.commit()
        await retag.net_refunds(s, force=True)
        t = await s.get(Transaction, "tx-cr")
        check("an answered credit keeps her category and status through a re-sweep",
              t.category == "want" and t.status == "enriched", f"{t.category}/{t.status}")

        print("\n[7] D-4 — the two invisible writers now leave receipts")
        # a job to point at
        await tools.run(s, "log_expense", {"amount": 30, "merchant": "道具店",
                                           "project": "測試案", "reimbursable": True})
        n0 = len(await changelog.recent(s, 100))
        out = await tools.run(s, "set_project_kind", {"project": "測試案", "kind": "portfolio"})
        chg = await changelog.recent(s, 100)
        check("set_project_kind writes an undoable Change row",
              out["ok"] and len(chg) == n0 + 1, str(out)[:60])
        undone = await changelog.undo(s, chg[0]["id"])
        from app import projects as PJ
        over = await PJ.overlay(s)
        check("…and undoing it restores the day-rate basis",
              undone["ok"] and (over.get("測試案", {}) or {}).get("kind") != "portfolio",
              str(over.get("測試案")))

        # match_refunds: row-level patches
        s.add(Transaction(id="tx-fare", account_id="apple", amount=-42.0,
                          merchant_desc="UBER TRIP", category="work", claim="sent",
                          reimbursable=True, project="測試案",
                          posted_at=now() - timedelta(days=3)))
        s.add(Transaction(id="tx-back", account_id="chase", amount=42.0,
                          merchant_desc="ZELLE FROM PRODUCTION", status="prompted",
                          posted_at=now()))
        await s.commit()
        out = await tools.run(s, "match_refunds", {})
        chg = await changelog.recent(s, 5)
        import json as _json
        patch = _json.loads((await s.get(Change, chg[0]["id"])).patch or "{}")
        check("match_refunds settles the pair", out["n_settled"] >= 1, str(out["n_settled"]))
        check("…and the Change row carries both mutated transactions",
              chg[0]["tool"] == "match_refunds" and len(patch.get("rows") or []) >= 2,
              f'{len(patch.get("rows") or [])} rows')
        undone = await changelog.undo(s, chg[0]["id"])
        fare = await s.get(Transaction, "tx-fare")
        back = await s.get(Transaction, "tx-back")
        check("…and undo puts the claim back to sent, unlinked",
              undone["ok"] and fare.claim == "sent" and back.nets_txn_id is None,
              f"{fare.claim}/{back.nets_txn_id}")

        print("\n[8] the WATCHED list guards keys that exist")
        check("cfg_budget_from is watched (the old entry was a typo aimed at nothing)",
              "cfg_budget_from" in changelog.WATCHED and "cfg_budget_start" not in changelog.WATCHED)
        check("cfg_projects is watched", "cfg_projects" in changelog.WATCHED)

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILED: " + ", ".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

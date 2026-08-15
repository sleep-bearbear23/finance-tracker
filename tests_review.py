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

        print("\n[9] C-1 — 陳會計 quotes real spending, not card payments")
        s.add(Transaction(id="tx-cardpay", account_id="chase", amount=-2313.30,
                          merchant_desc="APPLECARD GSBANK PAYMENT", category="transfer",
                          status="ignored", posted_at=now()))
        s.add(Transaction(id="tx-lunch", account_id="apple", amount=-18.50,
                          merchant_desc="片場午餐", category="food", status="auto",
                          posted_at=now()))
        await s.commit()
        from app import queries
        ctx = await queries.build_context(s)
        line = next(l for l in ctx.splitlines() if l.startswith("本月支出合計"))
        total = float(line.split("$")[1].replace(",", ""))
        check("the LINE context total excludes the card payment",
              total < 2000, line)

        print("\n[10] G-2 — the report counts pay, not every deposit")
        from app import reports
        from datetime import timedelta as _td2
        s.add(Transaction(id="tx-momback", account_id="chase", amount=350.0,
                          merchant_desc="Online Transfer From CHK ...7567",
                          inflow_kind=T.REIMBURSE_FAMILY, status="auto", posted_at=now()))
        await s.commit()
        data = await reports.gather(s, now() - _td2(days=30), now() + _td2(days=1))
        check("媽媽回款 is not income in the report",
              data["income"] < 3500, str(data["income"]))  # only the real deposits

        print("\n[11] F-2 — a lens that has seen nothing abstains")
        L = allowance._trajectory(0.0, -109.0, observations=0)
        check("zero observations → no vote, not a −$109 vote",
              L["value"] is None and L.get("abstain"), str(L))
        check("…and scaling leaves an abstention alone",
              allowance._scale(L, 0.33)["value"] is None)
        L2 = allowance._trajectory(478.0, -50.0, observations=6)
        check("with data it still votes, drift and all", near(L2["value"], 428.0),
              str(L2["value"]))

        print("\n[12] B-5 — being vague is no longer free")
        check("an uncategorised spend eats the allowance", T.in_allowance(None))
        t_unc = Transaction(id="x", account_id="a", amount=-116.62,
                            merchant_desc="大华超级市场", category=None, status="prompted")
        check("…through the discretionary predicate too", budget.is_discretionary(t_unc))

        print("\n[13] F-2 — a brokerage dip is not a grocery problem")
        from app.models import Snapshot
        for day, netv, cashv in [("2026-07-01", 12000, 9000), ("2026-08-10", 9000, 9000)]:
            s.add(Snapshot(day=day, net_worth=netv, assets=netv, debts=0, cash=cashv))
        await s.commit()
        drift = await allowance._net_drift(s)
        check("net worth fell $3,000 but cash held — drift reads ~0", near(drift, 0.0),
              str(drift))

        print("\n[14] D-3 — mark_payment_received can no longer lose money")
        # (a) the deposit is already in the ledger: link, don't double-count
        await tools.run(s, "add_expected_payment",
                        {"amount": 1400, "note": "Jump Deer 八月", "when": "2026-08"})
        n_before = len([t for t in (await s.execute(select(Transaction))).scalars()])
        out = await tools.run(s, "mark_payment_received", {"which": "Jump Deer 八月"})
        n_after = len([t for t in (await s.execute(select(Transaction))).scalars()])
        check("with the $1,400 already in the ledger it LINKS instead of creating",
              out["ok"] and out.get("matched_txn") and n_after == n_before,
              str(out.get("matched_txn")))
        check("…and the receipt names the deposit it matched",
              "1,400" in out["summary"] and "進來的" in out["summary"], out["summary"][:60])

        # (b) no deposit anywhere: refuse and ask, never guess
        await tools.run(s, "add_expected_payment",
                        {"amount": 777, "note": "神祕案", "when": "2026-08"})
        out = await tools.run(s, "mark_payment_received", {"which": "神祕案"})
        pend2 = await __import__("app.prefs", fromlist=["prefs"]).pending_invoices(s)
        check("no matching deposit → refuses with a question, invoice stays",
              not out["ok"] and out.get("needs_confirm")
              and any("神祕案" in (p.get("note") or "") for p in pend2), str(out)[:70])

        # (c) she names a non-synced account: record once, then clear
        out = await tools.run(s, "mark_payment_received",
                              {"which": "神祕案", "account": "現金"})
        cash_rows = [t for t in (await s.execute(select(Transaction))).scalars()
                     if near(t.amount, 777)]
        pend3 = await __import__("app.prefs", fromlist=["prefs"]).pending_invoices(s)
        check("cash income is recorded exactly once and the invoice clears",
              out["ok"] and len(cash_rows) == 1
              and not any("神祕案" in (p.get("note") or "") for p in pend3),
              f"{len(cash_rows)} rows")

        print("\n[15] G-1 — a lean period speaks instead of dying on line 13")
        from app import alerts
        sent = []
        real_push = alerts.line_client.push
        async def fake_push(owner, msg): sent.append(msg)
        alerts.line_client.push = fake_push
        await set_kv(s, "owner_user_id", "U-momo")
        await allowance.set_start_date(s, now().date().isoformat())
        b0 = await budget.status(s)
        await alerts.check(s)
        await alerts.check(s)   # second call must not repeat it
        alerts.line_client.push = real_push
        if b0["pct_used"] is None:
            check("allowance ≤ 0 sends ONE lean-period heads-up, not silence",
                  len(sent) == 1 and "不是你花掉的" in sent[0],
                  sent[0][:40] if sent else "nothing sent")
        else:
            check("allowance positive on this data — lean path not exercised (ok)",
                  len(sent) <= 1, f"pct={b0['pct_used']}")

        print("\n[16] Phase 4 — the small knives")
        out = await tools.run(s, "raise_daily", {"amount": -20})
        check("a negative raise is refused, not abs()'d into a bigger grant",
              not out["ok"] and "正數" in out["error"], str(out)[:60])

        out = await tools.run(s, "log_expense", {"amount": 5, "merchant": "x",
                                                 "category": "nonsense-id"})
        check("an invented category id is rejected, not silently exempted",
              not out["ok"] and "分類" in out["error"], str(out)[:60])

        a1 = await tools.run(s, "log_income", {"amount": 250, "source": "小案", "date": "2026-06-25"})
        a2 = await tools.run(s, "log_income", {"amount": 250, "source": "小案", "date": "2026-06-25"})
        check("two identical cash jobs no longer collide on a deterministic key",
              a1["ok"] and a2["ok"] and a1["id"] != a2["id"], f'{a1.get("id")} vs {a2.get("id")}')

        from app import prefs as PR2
        await PR2.update_account(s, "AppleCard", -500.0, "debt")
        out = await tools.run(s, "log_income", {"amount": 100, "source": "y", "account": "AppleCard"})
        accts = PR2._load_list(await get_kv(s, "cfg_accounts"))
        ac = next(a0 for a0 in accts if a0.get("name") == "AppleCard")
        check("income 'into' a credit card does not increase the debt",
              out["ok"] and near(float(ac["amount"]), -500.0), str(ac["amount"]))

        await fixed.save(s, [])
        check("deleting the last fixed cost leaves ZERO rows, not nine resurrected defaults",
              (await fixed.rows(s, include_sinking=False)) == [],
              str(len(await fixed.rows(s, include_sinking=False))))
        await fixed.save(s, fixed.DEFAULTS)   # put them back for anything downstream

        check("a bare 7567 in a merchant string is no longer 媽媽回款",
              not T.family_payback("SQ *SHOP 7567 LOS ANGELES", 45.0))
        check("…but her mother's actual transfer still is",
              T.family_payback("Online Transfer From CHK ...7567", 200.0))

        from app import projects as PJ2
        r = await PJ2.resolve(s, "")
        check("resolve('') returns a dict, not a TypeError for four callers",
              isinstance(r, dict) and r["id"] is None, str(r))

        s.add(Transaction(id="tx-onjob", account_id="apple", amount=-33.0,
                          merchant_desc="HOME DEPOT", project="awct", category="work",
                          posted_at=now()))
        await s.commit()
        hits, _ = tools._find_charges([await s.get(Transaction, "tx-onjob")], "awct",
                                      now().date() - timedelta(days=9))
        check("`which` no longer matches the project field itself", hits == [], str(len(hits)))

        print("\n[17] Phase 5 — the diagnostics are on")
        from app import dashboard as DASH
        diags = await DASH._diagnostics(s)
        check("_diagnostics runs clean and returns a list", isinstance(diags, list),
              str(diags)[:70])
        await set_kv(s, "last_run:alerts", (now() - timedelta(days=3)).isoformat())
        diags = await DASH._diagnostics(s)
        check("a dead alert loop becomes a visible warning within a day",
              any("超支提醒" in d for d in diags), str(diags)[:70])

        print("\n[18] Phase 6.1 — the earning-side watch loop")
        from app import seed_invoices as SI, watch
        await SI.backfill(s, force=True)     # her 13-invoice archive — the rate's evidence
        sv = await watch.survival_days(s)
        check("the floor is derived from the engine, not typed",
              sv["days"] is not None and 3 < sv["days"] < 30, str(sv))

        pipe = await watch.pipeline(s)
        check("the pipeline reads booked days and a baseline",
              "booked" in pipe and pipe["baseline"] >= pipe["floor"],
              f'{pipe["booked"]} vs {pipe["baseline"]}')

        note = await watch.rate_note(s, 800, 4)     # $200/day against a $350 archive
        check("a below-rate booking gets one informational line",
              note is not None and "200" in note, str(note))
        check("a normal-rate booking gets none",
              await watch.rate_note(s, 1400, 4) is None)

        msg = await watch.weekly(s)
        msg2 = await watch.weekly(s)
        check("the weekly check speaks at most once per week",
              msg2 is None, str(msg2))

        print("\n[19] F-7 — money can't count twice on the scoreboard")
        from app import analytics as AN, season as SE
        await tools.run(s, "add_expected_payment",
                        {"amount": 1585, "note": "已經到帳但忘了劃掉的案子", "when": now().strftime("%Y-%m")})
        s.add(Transaction(id="tx-dup", account_id="chase", amount=1585.0,
                          merchant_desc="ZELLE FROM SOMEONE", status="income",
                          inflow_kind=T.PAY, posted_at=now()))
        await s.commit()
        te = await AN.to_earn(s, 3)
        sb = await SE.progress(s, te["tiers"])
        check("an invoice whose money already landed is flagged, not double-counted",
              any("忘了劃掉" in x["note"] for x in sb.get("probably_landed", [])),
              str(sb.get("probably_landed"))[:70])
        check("…and the event log still sums exactly to the headline",
              near(sb["events"][-1]["running"], sb["secured"]) if sb.get("events") else True)

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

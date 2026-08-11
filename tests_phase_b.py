"""Phase B verification — against Momo's real balances and her real 19 months of history.

The point of the three-lens design is that Momo can interrogate the number. So these
checks are mostly "does the arithmetic she'd do by hand match what the code says", plus
the two rules that must never bend: expected income can't loosen the allowance, and a
deficit is never silently rounded up to zero.

    PYTHONPATH=. python3 tests_phase_b.py
"""
from __future__ import annotations

import asyncio
import json
import os
from datetime import date, datetime, timedelta
from pathlib import Path

os.environ.update(
    DATABASE_URL="sqlite+aiosqlite:////tmp/phaseb.db",
    ANTHROPIC_API_KEY="x", LINE_CHANNEL_ACCESS_TOKEN="x", LINE_CHANNEL_SECRET="x",
    DASHBOARD_TOKEN="tok",
)
Path("/tmp/phaseb.db").unlink(missing_ok=True)

from app import allowance as AL  # noqa: E402
from app import budget, cleanup, dashboard, migrate, prefs, retag  # noqa: E402
from app import fixed as FX  # noqa: E402
from app import period as P  # noqa: E402
from app import stability as STAB  # noqa: E402
from app import tax as TAX  # noqa: E402
from app.config import TZ, now  # noqa: E402
from app.db import Session, engine, init_db  # noqa: E402
from app.models import Account, Transaction  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{('  — ' + detail) if detail else ''}")


def near(a, b, eps=0.02):
    return abs((a or 0) - (b or 0)) < eps


async def seed():
    await init_db()
    await migrate.run(engine)
    raw = json.loads(Path("app/data/applecard.json").read_text())
    async with Session() as s:
        s.add(Account(id="chk", name="Chase Total Checking (1234)", org="Chase", balance=3120.55))
        s.add(Account(id="sd", name="Self-Directed (7435)", org="J.P. Morgan", balance=2774.10))
        for r in raw:
            s.add(Transaction(
                id=r["id"], account_id="applecard", amount=r["amount"], merchant_desc=r["desc"],
                posted_at=datetime.fromisoformat(r["date"]).replace(tzinfo=TZ),
                category=r.get("cat"), status="auto", source="applecard"))
        # a real pay deposit this quarter, so the tax reserve has something to bite on
        s.add(Transaction(id="pay-1", account_id="chk", amount=2800.0,
                          merchant_desc="ZELLE FROM AVIA GAMES",
                          posted_at=datetime(2026, 7, 20, tzinfo=TZ),
                          category=None, status="income", source="simplefin",
                          inflow_kind="pay"))
        await s.commit()
        await cleanup.ensure_accounts(s)
        # the balances Momo gave on 2026-08-11
        await prefs.update_account(s, "Apple Goldman Sachs Savings", 7472.63, "cash")
        await prefs.update_account(s, "Apple Card", 2626.01, "credit")
        await prefs.update_account(s, "Venmo", 145.28, "cash")
        await prefs.set_prefs(s, savings_amount=500.0, savings_cadence="biweekly")
        await retag.retag(s)
        await retag.net_refunds(s)
        await dashboard.write_snapshot(s)


async def main():
    await seed()
    key = budget.current_key()

    async with Session() as s:
        print("\n[1] fixed costs are rows, and they add to the figure we agreed")
        rows = await FX.rows(s)
        total = await FX.monthly_total(s)
        for r in rows:
            print(f"      {r['name'][:26]:28} {r['monthly']:>8.2f}/月  ({r['cadence']})")
        print(f"      {'合計':28} {total:>8.2f}/月")
        check("monthly total is the $1,521 we worked out", near(total, 1521.0, 3.0),
              f"{total}")
        check("GEICO is amortised, not charged whole",
              near(next(r["monthly"] for r in rows if "GEICO" in r["name"]), 884.28 / 6))
        check("Ultra Mobile is amortised over 6 months",
              near(next(r["monthly"] for r in rows if "Ultra" in r["name"]), 186 / 6))
        check("sinking funds are NOT in the agreed total",
              all(not r["sinking"] for r in rows))
        with_sink = await FX.monthly_total(s, include_sinking=True)
        check("turning sinking funds on raises it", with_sink > total,
              f"{total} -> {with_sink}")

        print("\n[2] the renewal calendar sees the lumps coming")
        cal = await FX.calendar(s, months=14)
        names = {c["name"] for c in cal}
        check("GEICO renewal is on the calendar", any("GEICO" in n for n in names))
        check("DMV 2027 renewal is on the calendar", any("DMV" in n for n in names))
        check("monthly lines are not spammed onto it",
              not any("Adobe" in n for n in names))
        check("calendar is in date order",
              cal == sorted(cal, key=lambda x: x["due"]))

        print("\n[3] tax is walled off, and the deadlines are the real ones")
        st = await TAX.status(s)
        check("rate defaults to 30%", near(st["rate"], 0.30))
        check("reserve = income × rate", near(st["should_hold"], st["earned_ytd"] * 0.30),
              f'{st["earned_ytd"]} -> {st["should_hold"]}')
        check("something is actually reserved", st["outstanding"] > 0, f'{st["outstanding"]}')
        nxt = TAX.next_deadline(date(2026, 8, 11))
        check("next deadline from 8/11 is Sep 15", nxt["due"] == "2026-09-15", str(nxt))
        check("California is 0% in September", nxt["ca_pct"] == 0.0)
        check("January is 30% for California",
              TAX.next_deadline(date(2026, 10, 1))["ca_pct"] == 0.30)
        check("June is California's 40% instalment",
              TAX.next_deadline(date(2027, 5, 1))["ca_pct"] == 0.40)
        check("the estimate carries its caveat", "會計師" in st["caveat"])

        print("\n[4] the ladder, from real survival burn")
        a = await AL.compute(s, key)
        surv = a["fixed_monthly"] + AL.LEAN_FLEX_MONTHLY
        check("survival burn ≈ $2,071", near(surv, 2071, 5), f"{surv:.2f}")
        rungs = {r["name"]: r["amount"] for r in a["ladder"]}
        check("第一階 = 1 month survival", near(rungs["第一階"], surv))
        check("第三階 = 3 months survival", near(rungs["第三階"], surv * 3))
        check("target is computed, not typed",
              a["emergency"]["pinned"] is False and a["emergency"]["months"] is not None,
              f'${a["emergency"]["target"]:,.0f} ≈ {a["emergency"]["months"]} months')
        check("target = months × survival burn",
              abs(rungs["目標"] - a["emergency"]["months"] * surv) < 260,
              f'{rungs["目標"]} vs {a["emergency"]["months"]}×{surv:.0f}')
        check("target never drops below 3 months", rungs["目標"] >= surv * 3 - 260)
        check("the target explains itself", len(a["emergency"]["why"]) >= 1)
        check("thin history falls back to the 3-month floor, and says so",
              a["emergency"]["months"] >= 3.0
              and (a["emergency"]["confidence"] != "low"
                   or "三個月" in " ".join(a["emergency"]["why"])))

        print("\n[4b] the emergency target is measured from instability")
        SURV = 2071.34
        steady = {f"2026-{m:02d}": 3000.0 for m in range(1, 13)}
        r = STAB.assess(steady, SURV)
        check("steady income → the 3-month floor", near(r["months"], 3.0, 0.05),
              f'{r["months"]} months = ${r["target"]:,.0f}')
        swingy = {f"2026-{m:02d}": (6000.0 if m % 2 else 200.0) for m in range(1, 13)}
        r2 = STAB.assess(swingy, SURV)
        check("wildly swinging income needs more", r2["months"] > r["months"],
              f'{r2["months"]} months = ${r2["target"]:,.0f}')
        dry = {f"2026-{m:02d}": (0.0 if m in (4, 5, 6) else 3000.0) for m in range(1, 13)}
        r3 = STAB.assess(dry, SURV)
        check("a three-month dry spell adds three months", r3["months"] >= 6.0,
              f'{r3["months"]} months')
        check("never more than 9 months",
              STAB.assess({f"2026-{m:02d}": (20000.0 if m == 1 else 0.0)
                           for m in range(1, 13)}, SURV)["months"] <= 9.0)
        check("never less than 3 months", r["months"] >= 3.0)
        check("every adjustment is itemised",
              set(r2["components"]) == {"base", "volatility", "drought"})

        print("\n[5] the reserve pile is the right money")
        # liquid cash − the card she owes − tax that isn't hers. Brokerage excluded.
        expect = round(3120.55 + 7472.63 + 145.28 - 2626.01 - st["outstanding"], 2)
        check("reserve = cash − card debt − tax held", near(a["reserve_total"], expect),
              f'{a["reserve_total"]} vs {expect}')
        check("the brokerage is not in it",
              a["reserve_total"] < 3120.55 + 7472.63 + 145.28)
        check("she knows which rung she is standing on", a["standing_rung"] is not None,
              str(a["standing_rung"]))

        print("\n[6] three lenses, smallest wins, and she names it")
        for L in a["lenses"]:
            print(f"      {L['name']}  ${L['value']:>9,.2f}   {L['why']}")
        print(f"      → 綁住的是「{a['binding']}」，最後給 ${a['allowance']:,.2f}")
        check("exactly three lenses", len(a["lenses"]) == 3)
        check("the spendable number is never negative", a["allowance"] >= 0,
              str(a["allowance"]))
        check("a negative lens reports a shortfall instead of setting the number",
              (a["shortfall"] <= 0) and
              (a["shortfall"] < 0) == any(L["value"] < 0 for L in a["lenses"][:1]),
              f'shortfall={a["shortfall"]}')
        check("spendable never exceeds what the cash supports",
              a["allowance"] <= max(0.0, a["lenses"][1]["value"]) + 0.01)
        check("a positive plan lens still caps it",
              a["lenses"][0]["value"] <= 0 or
              a["allowance"] <= a["lenses"][0]["value"] + 0.01)
        check("every lens explains itself", all(L["why"] for L in a["lenses"]))
        check("plan lens nets tax out first",
              near(a["income_after_tax"], a["income_period"] * (1 - st["rate"])))

        print("\n[7] the rule that must never bend: expected income cannot loosen it")
        before = (await AL.compute(s, key))["allowance"]
        await prefs.add_invoice(s, 9000.0, "2026-12", "一個很大的案子")
        after = (await AL.compute(s, key))["allowance"]
        check("booking a $9,000 gig does not raise the allowance", after <= before + 0.01,
              f"{before:.2f} -> {after:.2f}")
        far = (await AL.compute(s, key))["periods_to_money"]
        await prefs.add_invoice(s, 500.0, "2026-08", "下週就到的小案")
        soon = (await AL.compute(s, key))
        check("a sooner payment shortens the wait", soon["periods_to_money"] <= far,
              f'{far} -> {soon["periods_to_money"]}')
        check("…and that can only ever loosen the CUSHION lens, never the plan",
              soon["lenses"][0]["value"] <= a["lenses"][0]["value"] + 0.01,
              f'{a["lenses"][0]["value"]} -> {soon["lenses"][0]["value"]}')
        check("the plan lens says which income figure it used and why",
              bool(soon.get("income_basis_why")), str(soon.get("income_basis_why")))

        print("\n[8] shocks: 自己造成 is a debt, 無法避免 is not")
        base = (await AL.compute(s, key))["allowance"]
        await AL.add_shock(s, 400.0, AL.SELF, "停車罰單", periods=4)
        hit = await AL.compute(s, key)
        check("a self-inflicted $400 over 4 periods costs $100 this period",
              near(hit["shock_load"]["per_period"], 100.0),
              str(hit["shock_load"]["per_period"]))
        check("and it comes off the allowance", near(hit["allowance"], base - 100.0, 0.05),
              f"{base:.2f} -> {hit['allowance']:.2f}")
        await AL.add_shock(s, 800.0, AL.UNAVOIDABLE, "急診")
        unav = await AL.compute(s, key)
        check("an unavoidable shock does NOT become a repayment",
              near(unav["shock_load"]["per_period"], 100.0))
        for _ in range(6):
            await AL.add_shock(s, 400.0, AL.SELF, "又一張罰單", periods=4)
        loaded = await AL.compute(s, key)
        check("she calls it out when smoothing exceeds 15% of income",
              loaded["shock_load"]["over_cap"],
              f'{loaded["shock_load"]["per_period"]} vs cap {loaded["shock_load"]["cap"]}')

        print("\n[9] 起算日 — the first cadence is pro-rated, not backdated")
        full = await AL.compute(s, key)
        lo, hi = P.key_bounds(key)
        mid = lo + timedelta(days=P.days_in(key) // 2)
        await AL.set_start_date(s, mid)
        part = await AL.compute(s, key)
        check("coverage drops below 1", part["coverage"] < 1.0, str(part["coverage"]))
        check("it is flagged as partial", part["partial"])
        # `coverage` is rounded to 3dp for display, so compare with a cent or two of slack
        check("fixed costs pro-rate too",
              near(part["fixed_period"], full["fixed_period"] * part["coverage"], 1.0),
              f'{part["fixed_period"]} vs {full["fixed_period"] * part["coverage"]:.2f}')
        check("savings pro-rates too",
              near(part["savings_period"], full["savings_period"] * part["coverage"], 1.0))
        check("she says so in plain words",
              any("才開始算" in line for line in AL.explain(part)))
        check("spending before 起算日 is not charged",
              part["spent"] <= full["spent"] + 0.01,
              f'{full["spent"]} -> {part["spent"]}')
        await AL.set_start_date(s, lo)   # back to full coverage for the rest

        print("\n[9b] savings is soft — it gets skipped before spending gets cut")
        await prefs.set_prefs(s, savings_amount=500.0, savings_cadence="biweekly")
        soft = await AL.compute(s, key)
        if soft["income_after_tax"] - soft["fixed_period"] < 500:
            check("a lean period skips the savings contribution",
                  soft["savings_skipped"] > 0, str(soft["savings_skipped"]))
            check("…and the skipped amount is not still charged",
                  soft["savings_period"] < 500.0)
            check("she says it out loud as 存錢欠帳",
                  any("存錢欠帳" in x for x in AL.explain(soft)))
            check("cutting savings never digs a deeper hole",
                  soft["lenses"][0]["value"] >= -soft["fixed_period"] - 0.01)

        print("\n[10] a deficit is shown as a deficit")
        await prefs.set_prefs(s, fixed_monthly=1521.0)
        await FX.save(s, [{"name": "誇張的房租", "amount": 99999.0,
                           "cadence": "monthly", "cat": "rent"}])
        broke = await AL.compute(s, key)
        check("plan lens goes negative rather than clamping to 0",
              broke["lenses"][0]["value"] < 0, str(broke["lenses"][0]["value"]))
        check("the deficit is labelled", broke["deficit"])
        check("it says whether money is coming", broke["deficit_kind"] in ("timing", "structural"),
              str(broke["deficit_kind"]))
        check("the shortfall is reported as its own number",
              broke["shortfall"] < 0, str(broke["shortfall"]))
        check("but 可以花 stays at or above zero — a deficit is not a starvation order",
              broke["allowance"] >= 0, str(broke["allowance"]))
        check("she explains the deficit out loud",
              any("缺口" in line or "不夠" in line for line in AL.explain(broke)))
        check("…and says plainly it is not an instruction to stop eating",
              any("不是叫你別花" in line for line in AL.explain(broke)))
        await FX.save(s, FX.DEFAULTS)

        print("\n[11] she can say the whole thing in words")
        final = await AL.compute(s, key)
        lines = AL.explain(final)
        for line in lines:
            print(f"      {line}")
        check("explanation mentions every lens",
              all(any(L["name"] in x for x in lines) for L in final["lenses"]))
        check("no line leaks a raw category id",
              not any(t in " ".join(lines) for t in ("shopping", "snacks", "flex")))

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILED: " + ", ".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

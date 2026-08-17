"""有主的錢 — Batch A tests. The jar store, the one subtraction, seeding arithmetic,
release rules enforced server-side, drips, breach honesty, jar-funded exclusion.

    python3 tests_jars.py
"""
from __future__ import annotations

import asyncio
import os
from datetime import timedelta

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("ANTHROPIC_API_KEY", "x")
os.environ.setdefault("LINE_CHANNEL_ACCESS_TOKEN", "x")
os.environ.setdefault("LINE_CHANNEL_SECRET", "x")

from app import allowance, budget, changelog, jars, migrate  # noqa: E402
from app.config import now  # noqa: E402
from app.db import Session, engine, get_kv, init_db, set_kv  # noqa: E402
from app.models import Change, Transaction  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{('  — ' + detail) if detail else ''}")


def near(a, b, eps=0.02):
    return a is not None and b is not None and abs(a - b) <= eps


async def main() -> int:
    await init_db()
    await migrate.run(engine)

    async with Session() as s:
        print("\n[1] seeding — Momo's arithmetic, exactly")
        lines = await jars.seed(s, floor_amount=2233.0, gs_balance=7470.41,
                                tax_outstanding=2226.44, emergency_target=7000.0,
                                season_pot=0.0)
        check("seed produces the receipt lines", len(lines) >= 3, str(len(lines)))
        js = await jars.load(s)
        check("seven stored jars", len(js) == 7, str([j['id'] for j in js]))
        floor = jars.get(js, "floor")
        emerg = jars.get(js, "emergency")
        check("地板 seeds at today's rung", near(floor["balance"], 2233.0))
        check("緊急預備金 = GS − 稅 − 地板",
              near(emerg["balance"], 7470.41 - 2226.44 - 2233.0), str(emerg["balance"]))
        check("everything judgment-shaped starts at $0",
              all(near(jars.get(js, k)["balance"], 0.0)
                  for k in ("contingency", "dmv", "car", "season", "experiment")))
        lines2 = await jars.seed(s, floor_amount=999.0, gs_balance=1.0,
                                 tax_outstanding=0.0, emergency_target=1.0, season_pot=0.0)
        js2 = await jars.load(s)
        check("seeding twice does nothing twice",
              lines2 == [] and near(jars.get(js2, "floor")["balance"], 2233.0))

        print("\n[2] spoken_for — the one total")
        sf = await jars.spoken_for(s, tax_outstanding=2226.44)
        check("tax rides along as a computed jar",
              sf["jars"][0]["id"] == "tax" and near(sf["jars"][0]["balance"], 2226.44))
        expect_total = 2226.44 + 2233.0 + (7470.41 - 2226.44 - 2233.0)
        check("total = 稅 + 地板 + 預備金 (everything else $0)",
              near(sf["total"], expect_total), str(sf["total"]))
        check("reserve pots counted apart from goal pots",
              near(sf["reserve"], sf["total"] - 2226.44) and near(sf["goals"], 0.0))

        print("\n[3] release rules are the server's, not her manners")
        out = await jars.draw(s, "tax", 100.0)
        check("稅 refuses, always", out["ok"] is False and "國稅局" in out["error"])
        out = await jars.draw(s, "floor", 100.0)
        check("地板 refuses outside a timing dip", out["ok"] is False)
        out = await jars.draw(s, "floor", 100.0, dip_active=True, deficit_kind="structural")
        check("地板 refuses a structural hole even in a dip", out["ok"] is False)
        out = await jars.draw(s, "floor", 100.0, dip_active=True, deficit_kind="timing")
        check("地板 opens for a genuine timing bridge", out["ok"] is True, out.get("error", ""))
        out = await jars.draw(s, "emergency", 500.0, deficit_kind="timing")
        check("預備金 refuses a timing gap (that's the 地板's job)", out["ok"] is False)
        out = await jars.draw(s, "emergency", 500.0, deficit_kind="structural")
        check("預備金 refuses without a stated plan", out["ok"] is False and "計畫" in out["error"])
        out = await jars.draw(s, "emergency", 500.0, deficit_kind="structural",
                              plan="每期 $500，撐 4 期")
        check("預備金 opens for a structural deficit with a plan",
              out["ok"] is True and "計畫" in out["receipt"])
        out = await jars.draw(s, "contingency", 50.0)
        check("應急 refuses more than it holds, and says the max",
              out["ok"] is False and "$0.00" in out["error"])
        await jars.allocate(s, "contingency", 600.0)
        out = await jars.draw(s, "contingency", 50.0)
        check("應急 opens anytime once funded", out["ok"] is True)

        # restore for later sections
        await jars.allocate(s, "floor", 100.0)
        await jars.allocate(s, "emergency", 500.0)

        print("\n[4] drips — once per period, never past the target")
        logs = await jars.accrue(s, "2026-2H8")
        js = await jars.load(s)
        check("DMV and 修車 each drip one period's share",
              near(jars.get(js, "dmv")["balance"], 371.0 / 24)
              and near(jars.get(js, "car")["balance"], 1200.0 / 24), str(logs))
        await jars.accrue(s, "2026-2H8")
        js = await jars.load(s)
        check("same period twice = no second drip",
              near(jars.get(js, "dmv")["balance"], 371.0 / 24))
        await jars.accrue(s, "2026-1H9")
        js = await jars.load(s)
        check("next period drips again",
              near(jars.get(js, "dmv")["balance"], 2 * 371.0 / 24))

        print("\n[5] breach — cash below the jars is said out loud, 稅 last")
        sf = await jars.spoken_for(s, tax_outstanding=2000.0)
        b = jars.breach(sf["total"] + 100.0, sf)
        check("covered pool → no breach", b is None)
        b = jars.breach(sf["total"] - 700.0, sf)
        check("short pool names the eaten pots in draw order",
              b is not None and b["eaten"][0]["kind"] == "contingency"
              and not b["tax_breached"], str(b))
        b = jars.breach(500.0, sf)
        check("deep breach reaches 稅, and flags it loudest",
              b is not None and b["tax_breached"], str(b and b["eaten"]))

        print("\n[6] jar-funded charges leave the line alone")
        t = Transaction(id="tx-vet", account_id="chase", amount=-180.0,
                        merchant_desc="VCA ANIMAL HOSPITAL", category="pets",
                        status="enriched", posted_at=now())
        s.add(t)
        await s.commit()
        check("before funding, the vet bill is a spend", budget.is_spend(t) is True)
        out = await jars.fund_charge(s, t, "contingency")
        check("the pot pays and says so", out["ok"] is True and "不吃這期的額度" in out["receipt"])
        check("after funding, the line never sees it", budget.is_spend(t) is False)
        check("…but the category stays true", t.category == "pets")

        print("\n[7] cfg_jars is watched — a jar move is a Change row")
        async with changelog.watching(s, tool="jar_test", actor="test",
                                      source_text="測試：罐子動一下"):
            await jars.allocate(s, "experiment", 25.0)
        from sqlalchemy import select
        rows = (await s.execute(select(Change).where(Change.tool == "jar_test"))).scalars().all()
        check("the allocation left an undoable record", len(rows) == 1)

        print("\n[8] the engine — one subtraction, before and after seeding")
        # A fresh DB (unseeded) must behave exactly like the legacy engine.
        # We test on THIS seeded db that compute() uses the jars total.
        res = await allowance.compute(s)
        sf_now = await jars.spoken_for(s, tax_outstanding=res["tax"]["outstanding"])
        check("compute() carries the spoken_for block",
              near(res["spoken_for"]["total"], sf_now["total"]))
        cush = next(L for L in res["lenses"] if L["name"] == "水位")
        check("水位 why-string names 有主的錢, not the old rung",
              "有主的錢" in cush["why"], cush["why"])
        check("available = pool − spoken_for",
              near(res["available"],
                   round(res["reserve_total"] - sf_now["reserve"], 2)), str(res["available"]))
        check("defended_floor now reports the 地板 jar",
              near(res["defended_floor"], jars.get(await jars.load(s), "floor")["balance"]))

        print("\n[10] the LINE tools — receipts, rules, and disambiguation")
        from app import tools
        check("three jar tools registered",
              all(n in tools.NAMES for n in ("jar_allocate", "jar_draw", "jar_set")))
        out = await tools.run(s, "jar_allocate", {"jar": "應急", "amount": 40.0},
                              source_text="放 $40 進應急")
        check("allocate lands with a receipt", out["ok"] is True and "短期應急" in out["receipt"])
        out = await tools.run(s, "jar_draw", {"jar": "稅", "amount": 10.0})
        check("稅 refuses through the tool too", out["ok"] is False)
        out = await tools.run(s, "jar_allocate", {"jar": "急", "amount": 5.0})
        check("an ambiguous name returns a numbered list and writes nothing",
              out["ok"] is False and out.get("needs_pick") is True, out.get("error", "")[:60])
        out = await tools.run(s, "jar_set", {"jar": "dmv", "annual": 400.0})
        check("jar_set retunes a drip", out["ok"] is True)
        js = await jars.load(s)
        check("…and the annual actually changed", near(jars.get(js, "dmv")["annual"], 400.0))
        from sqlalchemy import select as _sel
        n_changes = len((await s.execute(
            _sel(Change).where(Change.tool.in_(("jar_allocate", "jar_set"))))).scalars().all())
        check("every tool write is a Change row", n_changes >= 2, str(n_changes))

        print("\n[11] explain() says the spoken-for sentence")
        res = await allowance.compute(s)
        lines = allowance.explain(res)
        check("one sentence names 有主的錢 and the water that's left",
              any("有主的錢" in ln for ln in lines))

    # legacy window: a second, unseeded database — the engine must read tax + rung only
    print("\n[9] the deploy→重掃歷史 window behaves like the old engine")
    import sqlalchemy
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    eng2 = create_async_engine("sqlite+aiosqlite:///:memory:")
    from app.models import Base
    async with eng2.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    S2 = async_sessionmaker(eng2, expire_on_commit=False)
    async with S2() as s2:
        sf = await jars.spoken_for(s2, tax_outstanding=1000.0, legacy_floor=2233.0)
        check("unseeded store substitutes the legacy rung as a virtual 地板",
              near(sf["total"], 3233.0) and sf["jars"][1].get("legacy") is True,
              str(sf["total"]))

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILED: " + ", ".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

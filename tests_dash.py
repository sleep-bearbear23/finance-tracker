"""Dashboard consistency — the blocks must agree with each other, not just look right.

Momo's report: "my investment account disappears, and data in accounts and net asset
blocks aren't matching… the dash should be a whole database back end and each block
draws from the same database."

So these checks don't test one endpoint at a time. They call the real routes and
cross-compare the numbers a person would read side by side on the screen.

    PYTHONPATH=. python3 tests_dash.py
"""
from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timedelta
from pathlib import Path

os.environ.update(
    DATABASE_URL="sqlite+aiosqlite:////tmp/dashtest.db",
    ANTHROPIC_API_KEY="x", LINE_CHANNEL_ACCESS_TOKEN="x", LINE_CHANNEL_SECRET="x",
    DASHBOARD_TOKEN="tok",
)
Path("/tmp/dashtest.db").unlink(missing_ok=True)

from fastapi.testclient import TestClient  # noqa: E402

from app import cleanup, dashboard, migrate, prefs, retag  # noqa: E402
from app import facts as F  # noqa: E402
from app.config import TZ  # noqa: E402
from app.db import Session, engine, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Account, Transaction  # noqa: E402

PASS, FAIL = [], []
EPS = 0.011


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{('  — ' + detail) if detail else ''}")


def near(a, b):
    return abs((a or 0) - (b or 0)) < EPS


async def seed():
    await init_db()
    await migrate.run(engine)
    raw = json.loads(Path("app/data/applecard.json").read_text())
    async with Session() as s:
        # Momo's real shape: two synced Chase accounts (one of them a brokerage) plus
        # three manual ones she reports herself.
        s.add(Account(id="chk", name="Chase Total Checking (1234)", org="Chase", balance=3120.55))
        s.add(Account(id="sd", name="Self-Directed (7435)", org="J.P. Morgan", balance=2774.10))
        for r in raw:
            s.add(Transaction(
                id=r["id"], account_id="applecard", amount=r["amount"], merchant_desc=r["desc"],
                posted_at=datetime.fromisoformat(r["date"]).replace(tzinfo=TZ),
                category=r.get("cat"), status="auto", source="applecard"))
        # Pay, in the shape her bank actually reports it: one client under two spellings,
        # a deliberate empty month between deposits, and a payer string wrapped in Zelle
        # boilerplate. The 收入 page has to survive all three.
        today = datetime.now(TZ)
        for i, (days, amt, desc) in enumerate((
                (190, 900.0, "AVG MAY Payroll"),
                (160, 600.0, "AVG Payroll APRIL"),
                (40, 1400.0, "ZELLE PAYMENT FROM JUMP DEER MEDIA, INC. 30152107438"))):
            s.add(Transaction(id=f"pay{i}", account_id="chk", amount=amt, merchant_desc=desc,
                              posted_at=today - timedelta(days=days),
                              status="income", inflow_kind="pay", source="simplefin"))
        await s.commit()
        await prefs.add_invoice(s, 2300.0, when=today.strftime("%Y-%m"), note="某支片 尾款")
        await cleanup.ensure_accounts(s)
        await prefs.update_account(s, "Apple Goldman Sachs Savings", 7472.63, "cash")
        await prefs.update_account(s, "Apple Card", 2626.01, "credit")
        await prefs.update_account(s, "Venmo", 145.28, "cash")
        await retag.retag(s)
        await retag.net_refunds(s)
        await dashboard.write_snapshot(s)


async def main():
    await seed()
    app.router.lifespan_context = None
    c = TestClient(app)
    g = lambda p: c.get(p if "?" in p else p + "?t=tok",   # noqa: E731
                        params=None).json()

    ov = g("/api/overview")
    acc = g("/api/accounts")
    tr = g("/api/trends")
    led = g("/api/ledger?t=tok&limit=500")

    print("\n[1] the audit the app runs on itself")
    for name, payload in (("overview", ov), ("accounts", acc), ("trends", tr)):
        check(f"{name} reports no internal disagreement", not payload.get("audit"),
              "; ".join(payload.get("audit") or []))

    print("\n[2] the investment account is visible everywhere it should be")
    names = [a["name"] for a in acc["accounts"]]
    check("Self-Directed is in the accounts list", any("Self-Directed" in n for n in names),
          str(names))
    check("it is typed as invest, not cash",
          any(a["kind"] == "invest" for a in acc["accounts"]))
    check("net-worth rows list it too",
          any("Self-Directed" in r["name"] for r in ov["accounts"]))
    check("accounts card and net-worth card list the SAME accounts",
          sorted(names) == sorted(r["name"] for r in ov["accounts"]),
          f"{sorted(names)} vs {sorted(r['name'] for r in ov['accounts'])}")

    print("\n[3] every total agrees with every other total")
    check("accounts.cash_total == overview.spendable", near(acc["cash_total"], ov["spendable"]),
          f'{acc["cash_total"]} vs {ov["spendable"]}')
    check("accounts.invest_total == overview.invest", near(acc["invest_total"], ov["invest"]))
    check("accounts.debt_total == overview.debts", near(acc["debt_total"], ov["debts"]))
    check("accounts.net == overview.net_worth", near(acc["net"], ov["net_worth"]))
    listed = sum(a["balance"] * (-1 if a["kind"] == "credit" else 1) for a in acc["accounts"])
    check("the account rows literally add up to net worth", near(listed, ov["net_worth"]),
          f"{listed:.2f} vs {ov['net_worth']}")
    check("spendable excludes the brokerage",
          near(ov["assets"], ov["spendable"] + ov["invest"]))
    check("runway discounts the brokerage but not cash",
          near(ov["runway_net"],
               ov["spendable"] + ov["invest"] * ov["haircut"] - ov["debts"]))

    print("\n[4] one definition of spending, in every chart")
    flows_spend = sum(f["spend"] for f in tr["flows"])
    months = {m["month"]: m for m in tr["monthly"]}
    # only months where BOTH halves are inside the 12-period window are comparable —
    # the oldest month in the window is half-covered by construction
    halves: dict[str, int] = {}
    for fl in tr["flows"]:
        halves[fl["key"][:7]] = halves.get(fl["key"][:7], 0) + 1
    overlap = [m for m in months if halves.get(m) == 2]
    monthly_spend = sum(months[m]["spend"] for m in overlap)
    flows_in_overlap = sum(f["spend"] for f in tr["flows"] if f["key"][:7] in overlap)
    check("monthly bars == half-month flows over the same months",
          near(monthly_spend, flows_in_overlap),
          f"{monthly_spend:.2f} vs {flows_in_overlap:.2f}")
    check("flows total is a real number", flows_spend > 0, f"{flows_spend:.2f}")
    check("no half-month shows negative spending",
          all(f["spend"] >= -EPS for f in tr["flows"]),
          str([f["key"] for f in tr["flows"] if f["spend"] < 0]))
    check("category breakdown carries Chinese labels",
          all(x.get("label") for x in tr["category_spend"]))

    print("\n[5] the ledger agrees with the charts")
    async with Session() as s:
        f = await F.build(s)
    check("ledger row count == transactions in the registry",
          led["matched"] == len(f.txns), f'{led["matched"]} vs {len(f.txns)}')
    check("ledger total_out == facts all-time spend",
          near(led["total_out"], f.spend_in(f.txns and min(filter(None,
               (__import__("app.budget", fromlist=["x"]).eff_date(t) for t in f.txns))),
               f.today)),
          f'{led["total_out"]}')

    print("\n[6] per-account pages agree with the account list")
    for a in acc["accounts"]:
        d = g(f"/api/account?t=tok&id={a['id']}")
        check(f"{a['name'][:22]}: balance matches the list",
              near(d["account"]["balance"], a["balance"]))
        check(f"{a['name'][:22]}: no negative half-month",
              all(x["spend"] >= -EPS for x in d["series"]))

    print("\n[7] the 收入 page — a projection has to be honest about what it knows")
    from app import analytics as AN                                    # noqa: PLC0415
    inc = g("/api/income2")
    check("income2 reports no internal disagreement", not inc.get("audit"),
          "; ".join(inc.get("audit") or []))

    # the bank's packaging comes off, so one client is one row
    check("AVG MAY Payroll and AVG Payroll APRIL are the same payer",
          AN.payer_name("AVG MAY Payroll") == AN.payer_name("AVG Payroll APRIL") == "AVG",
          f'{AN.payer_name("AVG MAY Payroll")!r} / {AN.payer_name("AVG Payroll APRIL")!r}')
    check("a name that is nothing but noise keeps its original text",
          AN.payer_name("Payroll") == "Payroll")
    check("payer rows are unique", len({p["name"] for p in inc["payers"]}) == len(inc["payers"]))

    # a dry month is a month
    span = AN._month_span(["2026-01"])
    check("month span fills the gaps", "2026-06" in span and span[0] == "2026-01")
    check("month span stops at today, it does not run into the future",
          span[-1] == datetime.now(TZ).strftime("%Y-%m"), span[-1])
    mkeys = [m["month"] for m in inc["months"]]
    check("the month series has no holes in it",
          all(b == AN._month_span([a])[1] for a, b in zip(mkeys, mkeys[1:])), str(mkeys))
    here = datetime.now(TZ).strftime("%Y-%m")
    check("the half-finished month is charted but does not vote in the median",
          here in mkeys, str(mkeys[-1]))

    pj = inc["projection"]
    check("projection covers three months starting with this one",
          len(pj["months"]) == 3 and pj["months"][0]["month"] == here)
    check("likely is max(booked, typical) — never their sum",
          all(near(m["likely"], max(m["booked"], m["typical"])) for m in pj["months"]),
          str([(m["booked"], m["typical"], m["likely"]) for m in pj["months"]]))
    check("the projected total is the sum of the months",
          near(pj["likely_total"], sum(m["likely"] for m in pj["months"])))
    check("nothing is projected below what is already banked",
          all(m["likely"] >= m["booked"] - EPS for m in pj["months"]))

    ye = pj["year_end"]
    check("year-end estimate = banked + still to come",
          near(ye["estimate"], ye["so_far"] + ye["to_come"]))
    check("year-end never lands under what is already banked", ye["estimate"] >= ye["so_far"])
    this_year = next((y["amount"] for y in inc["years"] if y["year"] == here[:4]), 0.0)
    check("so_far is exactly this year's income, not a re-derivation",
          near(ye["so_far"], this_year), f'{ye["so_far"]} vs {this_year}')

    check("待收款 on 收入 == 待收款 on the overview",
          near(inc["pending"]["total"], ov["pending"]["total"]),
          f'{inc["pending"]["total"]} vs {ov["pending"]["total"]}')
    check("the index on 收入 == the index on 計畫",
          near(inc["to_earn"]["tiers"][0]["need"],
               g("/api/plan")["to_earn"]["tiers"][0]["need"]))

    print("\n[8] booked work: one landing date, and expectation that never flatters")
    from datetime import date as _date  # noqa: PLC0415

    from app import prefs as _prefs  # noqa: PLC0415

    # A shoot that wraps 9/14 is September WORK and October MONEY. Three modules used to
    # answer this differently — calendar said 10/14, the income page said September, the
    # horizon test said 10/1 — so the same job appeared in two months at once.
    sept = {"when": "2026-09", "amount": 2800.0, "note": "Avia 九月檔期"}
    check("with no wrap date, the clock starts at the end of the work month",
          _prefs.landing(sept) == _date(2026, 9, 30) + timedelta(days=_prefs.PAY_LAG_DAYS),
          str(_prefs.landing(sept)))
    check("a real wrap date beats the month — 9/2 and 9/28 are not the same money",
          _prefs.landing({"when": "2026-09", "wrapped_on": "2026-09-02"})
          < _prefs.landing({"when": "2026-09", "wrapped_on": "2026-09-28"}))
    check("an invoice with no month has no landing at all",
          _prefs.landing({"amount": 1.0}) is None)

    today = _date(2026, 8, 11)
    # Not-yet-due is deliberately under 1.0: an invoice that is not late *yet* is still not
    # money. How far under now depends on how much risk the job has actually retired.
    booked = {"when": "2026-09"}
    wrapped = {"when": "2026-09", "stage": "wrapped", "wrapped_on": "2026-09-14"}
    invoiced = {"when": "2026-09", "stage": "invoiced", "wrapped_on": "2026-09-14"}
    check("a shoot that has not happened is the riskiest kind of money",
          near(_prefs.confidence(booked, today), 0.70),
          str(_prefs.confidence(booked, today)))
    check("wrapping retires the production risk",
          near(_prefs.confidence(wrapped, today), 0.90))
    check("invoicing retires a little more",
          near(_prefs.confidence(invoiced, today), 0.95))
    check("stage and lateness compound — a booked job that is also overdue is worse than either",
          _prefs.confidence({"when": "2026-04"}, today)
          < min(0.70, _prefs.confidence({"when": "2026-04", "stage": "invoiced"}, today)),
          str(_prefs.confidence({"when": "2026-04"}, today)))
    check("nothing owed is ever worth literally zero",
          _prefs.confidence({"when": "2020-01"}, today) >= _prefs.CONFIDENCE_FLOOR)
    check("her own read on a production beats stage and lateness both",
          near(_prefs.confidence({"when": "2026-01", "confidence": 1.0}, today), 1.0))
    check("a day rate comes out of the day counts she already says out loud",
          near(_prefs.day_rate([{"amount": 2800, "days": 8}])["rate"], 350.0))
    check("lateness keeps eating it after the stage has had its say",
          0.95 > _prefs.confidence({"when": "2026-05", "stage": "invoiced"}, today)
          > _prefs.confidence({"when": "2026-02", "stage": "invoiced"}, today),
          f'5月 {_prefs.confidence({"when": "2026-05", "stage": "invoiced"}, today)} / '
          f'2月 {_prefs.confidence({"when": "2026-02", "stage": "invoiced"}, today)}')
    check("no date at all cannot be planned around",
          near(_prefs.confidence({"amount": 1.0}, today), 0.0))
    check("the haircut only ever shrinks the total",
          _prefs.believable([{"when": "2026-05", "amount": 1000.0}], today) < 1000.0)

    cal = g("/api/calendar")
    inc_rows = [i for i in cal["items"] if i["kind"] == "income"]
    pj2 = g("/api/income2")["projection"]
    if inc_rows:
        land_months = {i["date"][:7] for i in inc_rows}
        proj_months = {m["month"] for m in pj2["months"] if m["booked"] > 0}
        check("行事曆 and 收入 book the same money in the same month",
              not (proj_months - land_months - {here}),
              f"projection {sorted(proj_months)} vs calendar {sorted(land_months)}")

    te2 = g("/api/plan")["to_earn"]
    check("the index reports face value and believable value separately",
          te2["pending_face"] >= te2["pending"],
          f'{te2["pending_face"]} vs {te2["pending"]}')
    check("tax is applied BEFORE pending is deducted, not after",
          all(near(t["bare"], t["net"] / (1 - te2["tax_rate"])) for t in te2["tiers"]),
          str([(t["net"], t["bare"]) for t in te2["tiers"]]))
    check("every tier carries a floor that owes nothing to unpaid invoices",
          all(t["bare"] >= t["need"] for t in te2["tiers"]))
    check("the floor is never zero while she still has to eat",
          all(t["bare"] > 0 for t in te2["tiers"]),
          str([t["bare"] for t in te2["tiers"]]))

    print("\n[9] two clocks: this season is settled, next season is bookable")
    plan = g("/api/plan")
    sm, tb = plan["settlement"], plan["to_book"]

    # Payment lands ~45 days after wrap, so cash arriving in a season was earned in the one
    # before it, and work booked today lands in the next. Asking "earn more THIS season"
    # near its end is asking about a race already run.
    check("the settlement covers the tax period we are standing in",
          sm["start"] <= datetime.now(TZ).date().isoformat() <= sm["end"],
          f'{sm["start"]}–{sm["end"]}')
    check("the settlement is a result, not a target — it has no goal to hit",
          "tiers" not in sm and "net" in sm, str(list(sm))[:80])
    check("cash out = what was spent + what leaves by hand + what is left to spend",
          near(sm["out_total"],
               sm["spend_actual"] + sm["spend_by_hand"] + sm["spend_projected"]))
    check("cash in = landed + what is still due to arrive before the end",
          near(sm["in_total"], sm["cash_in"] + sm["cash_in_more"]))
    check("the verdict follows the arithmetic",
          (sm["verdict"] == "short") == (sm["net"] <= -200), f'{sm["verdict"]} @ {sm["net"]}')
    check("work done and not yet paid is reported next to the hole",
          sm["unpaid_weighted"] <= sm["unpaid_face"],
          f'{sm["unpaid_face"]} face / {sm["unpaid_weighted"]} weighted')

    check("the booking target aims at the window today's work can actually reach",
          tb["start"] > sm["start"], f'{sm["start"]}… → {tb["start"]}…')
    check("…which is a whole tax period, not a rolling window",
          tb["end"] >= tb["start"] and tb["months"] >= 1.9, f'{tb["months"]} months')
    check("its gap is what is needed minus what is already booked into that window",
          all(near(t["gap"], max(0.0, t["gross"] - tb["covered"])) for t in tb["tiers"]),
          str([(t["gross"], t["gap"]) for t in tb["tiers"]]))
    check("the deadline is the window's end minus the payment lag",
          tb["wrap_by"] < tb["end"], f'{tb["wrap_by"]} vs {tb["end"]}')
    check("the days it asks for fit in the days it gives her",
          all(t["work_days"] is None or t["work_days"] <= tb["days_to_book"]
              for t in tb["tiers"][:2]),
          str([(t["name"], t["work_days"]) for t in tb["tiers"]]) + f' in {tb["days_to_book"]}d')
    check("every booking counted toward the target lands inside the target's window",
          all(tb["start"] <= c["lands"] <= tb["end"] for c in tb["covered_items"]),
          str([c["lands"] for c in tb["covered_items"]]))
    # Momo: "the money as they come in is gonna be more than expected, so that number is
    # gonna drop once those invoices start getting paid right?" It has to. Counting only
    # PENDING invoices meant a payment deleted the pending row and the real deposit was
    # invisible, so getting paid pushed 「還要接」 UP — 35.9 shoot days became 41.6.
    check("money already in the bank counts toward the target too",
          near(tb["covered"], tb["landed"] + tb["booked"]),
          f'{tb["covered"]} = {tb["landed"]} landed + {tb["booked"]} booked')
    check("a paid invoice is worth its full face, not its discounted value",
          all(near(c["weighted"], c["amount"]) for c in tb["covered_items"]
              if c["stage"] == "paid"),
          str([(c["amount"], c["weighted"]) for c in tb["covered_items"]]))

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILED: " + ", ".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

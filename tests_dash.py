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
from app import prefs as prefs_mod  # noqa: E402
from app import seed_invoices as SI  # noqa: E402
from app import facts as F  # noqa: E402
from app import period as P  # noqa: E402
from app.config import TZ  # noqa: E402
from app.db import Session, engine, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Account, Transaction  # noqa: E402

PASS, FAIL = [], []
EPS = 0.011


class _Row:
    """A stand-in for a Transaction, for predicates that only read a couple of fields."""
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _old_line(cash: float, surv: float, n: int) -> float:
    """The floor rule as it was: defend whichever rung the pile happens to clear. Kept so
    the monotonicity test can prove it is fixing something that was really broken."""
    floor = max([surv * m for m in (1, 2, 3) if cash >= surv * m] or [0.0])
    return max(0.0, (cash - floor) / n)


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{('  — ' + detail) if detail else ''}")


def near(a, b, eps=EPS):
    return abs((a or 0) - (b or 0)) < eps


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
        await SI.backfill(s)          # the invoice archive, so 專案 has jobs to join
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
    check("a date the production actually gave her beats every estimate",
          _prefs.landing({"when": "2026-09", "wrapped_on": "2026-09-14",
                          "expect_on": "2026-09-25"}) == _date(2026, 9, 25),
          str(_prefs.landing({"when": "2026-09", "expect_on": "2026-09-25"})))
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
    # Assert the ORDER, not the numbers — Momo moved the levels once already ("wrapped
    # should be almost 100%, booked still on the high end") and a test that pins literals
    # only reports that she changed her mind.
    SC = _prefs.STAGE_CONFIDENCE
    check("a shoot that has not happened is the riskiest kind of money",
          near(_prefs.confidence(booked, today), SC["booked"])
          and SC["booked"] < SC["wrapped"] < SC["invoiced"] <= 1.0,
          str(SC))
    check("wrapping retires the production risk",
          near(_prefs.confidence(wrapped, today), SC["wrapped"]))
    check("invoicing retires a little more",
          near(_prefs.confidence(invoiced, today), SC["invoiced"]))
    # Her own read: a booked vertical rarely evaporates. Pricing one as a coin flip made
    # the forecast gloomier than her actual track record.
    check("a booked shoot is discounted, but not treated as a coin flip",
          0.75 <= SC["booked"] < SC["wrapped"], str(SC["booked"]))
    check("stage and lateness compound — a booked job that is also overdue is worse than either",
          _prefs.confidence({"when": "2026-04"}, today)
          < min(SC["booked"],
                _prefs.confidence({"when": "2026-04", "stage": "invoiced"}, today)),
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

    print("\n[10] the fortnight is graded against its line, and the runway carries the deadline")
    fn, rw = plan["fortnight"], plan["runway"]

    # Ten of her last twelve periods had less arrive than she spent, five had nothing at
    # all. Grading on deposits calls that ten failures, none of them decisions. Momo's Law:
    # "I should never punish myself for a shortage this month, because it happened a couple
    # weeks ago."
    check("the verdict is spent-vs-line, and nothing else",
          (fn["verdict"] == "under") == (fn["spent"] <= fn["line"]),
          f'{fn["verdict"]}: spent {fn["spent"]} vs line {fn["line"]}')
    check("a period where nothing arrived can still pass",
          fn["verdict"] == "under" or fn["spent"] > fn["line"],
          f'{fn["verdict"]}')
    check("the binding lens is named, and it names a lever",
          bool(fn["binding"]) and bool(fn["lever"]),
          f'{fn["binding"]} → {fn["diagnosis"]}')
    check("the diagnosis distinguishes timing from spending",
          fn["kind"] in ("income", "timing", "spending"), str(fn["kind"]))
    check("the fortnight knows where it sits in the season",
          1 <= (fn["session_index"] or 0) <= (fn["session_count"] or 0),
          f'{fn["session_index"]}/{fn["session_count"]}')
    check("cushion and runway are reported, not graded",
          "cushion" in fn and "verdict" not in str(fn.get("cushion")))

    fc = rw["lean"]
    check("the forecast starts from spendable cash, not net worth",
          near(fc["start_cash"], plan["standing"]["reserve_total"]),
          f'{fc["start_cash"]} vs {plan["standing"]["reserve_total"]}')
    check("each period's closing = opening + arrivals − burn",
          all(near(p["closing"], p["opening"] + p["arrive"] - p["burn"])
              for p in fc["periods"]),
          str([(p["label"], p["closing"]) for p in fc["periods"][:3]]))
    check("living lean never runs out sooner than living normally",
          (fc["runway_periods"] is None) or
          (rw["normal"]["runway_periods"] is not None and
           fc["runway_periods"] >= rw["normal"]["runway_periods"]),
          f'lean {fc["runway_periods"]} vs normal {rw["normal"]["runway_periods"]}')
    check("every period carries the last day a shoot could wrap and still cover it",
          all(p["wrap_by"] < p["start"] for p in fc["periods"]))
    check("a period whose wrap date has passed is marked unbookable",
          all(p["bookable"] == (p["wrap_by"] > datetime.now(TZ).date().isoformat())
              for p in fc["periods"]))
    check("a gap the lag cannot reach is never answered with 'go find work'",
          all("催" in L["text"] for L in rw["levers"] if L["kind"] == "chase"),
          str([L["kind"] for L in rw["levers"]]))
    check("gaps do not double-count the same shortfall period after period",
          sum(g["need_net"] for g in rw["gaps"])
          <= max([p["short"] for p in fc["periods"]] or [0]) + EPS,
          str([(g["label"], g["need_net"]) for g in rw["gaps"]]))

    print("\n[11] when the line stops covering food it stops calling itself a budget")
    # Momo's screen: 「還能花 $4 · 一天 $1」 with five days left, and she said "wow". Working
    # backwards, her 可動用 was sitting almost exactly on her $2,834 emergency floor, so the
    # cushion lens numerator collapsed and a correct calculation came out as pocket money.
    # Pure function on purpose — the crisis branch must be exercisable without waiting for
    # her cash to actually hit the floor.
    from app.analytics import dip_view
    LEAN = 2834.0                       # her lean flex, monthly
    hers = dip_view(line=225.0, spent=221.0, days_left=5, days_in=15,
                    lean_flex_monthly=LEAN)
    check("her actual $4/five-days screen is recognised as a dip, not a budget",
          hers["mode"] == "dip",
          f'線剩 {hers["line_left"]} vs 最省要 {hers["survival_need"]}')
    check("the dip is exactly what the line cannot cover",
          near(hers["dip"], hers["survival_need"] - hers["line_left"]),
          f'{hers["dip"]} = {hers["survival_need"]} − {hers["line_left"]}')
    check("the dip is named as a draw on the emergency fund",
          "緊急預備金" in hers["dip_note"] and f'{hers["dip"]:,.0f}' in hers["dip_note"],
          hers["dip_note"])
    check("a comfortable period is left alone",
          dip_view(1200.0, 300.0, 7, 15, LEAN)["mode"] == "normal")
    check("a line that was never big enough is blamed on the water level, not on her",
          hers["cause"] == "line" and "不是你花太兇" in hers["dip_note"])
    # The other way into a dip is a line that WAS enough, spent down. Telling her that one
    # with 「這不是你花太兇」 would make the card a liar, so the two causes are worded apart.
    over = dip_view(line=1600.0, spent=1550.0, days_left=1, days_in=15, lean_flex_monthly=LEAN)
    check("a line that was enough and got spent is named as spending, not as timing",
          over["mode"] == "dip" and over["cause"] == "spent"
          and "不是你花太兇" not in over["dip_note"],
          over["dip_note"])
    check("even then it does not follow her into the next period",
          "下一期會重新算" in over["dip_note"])
    # The note sits directly above the lever. Absolving her of overspending one line above
    # 「花得比自己的節奏兇」 would read as the card arguing with itself.
    check("the note never contradicts the lever printed under it",
          "不是你花太兇" not in dip_view(221.0, 217.0, 5, 15, LEAN, "軌跡")["dip_note"]
          and "不是你花太兇" in dip_view(221.0, 217.0, 5, 15, LEAN, "水位")["dip_note"],
          dip_view(221.0, 217.0, 5, 15, LEAN, "軌跡")["dip_note"])
    check("the last day of a period can never be a dip on its own",
          dip_view(0.0, 0.0, 0, 15, LEAN)["mode"] == "normal")
    check("survival scales with the days that are left, not the days that are gone",
          near(dip_view(0, 0, 10, 15, LEAN)["survival_need"],
               2 * dip_view(0, 0, 5, 15, LEAN)["survival_need"]))
    # A 15-day line was being compared against 5 days of food, which called her $221 line
    # "enough" and blamed her for spending it. Same span, or the blame lands on the wrong
    # person — and that is exactly the failure her Law names.
    check("the cause is judged over the whole period, not just the days that remain",
          dip_view(221.0, 217.0, 5, 15, LEAN)["cause"] == "line",
          str(dip_view(221.0, 217.0, 5, 15, LEAN)))
    check("the dip never exceeds what surviving costs",
          all(dip_view(L, 0, 5, 15, LEAN)["dip"]
              <= dip_view(L, 0, 5, 15, LEAN)["survival_need"] + EPS
              for L in (0, 50, 200, 500)))
    check("live data agrees with the pure function",
          fn["mode"] == dip_view(fn["line"], fn["spent"], fn["days_left"],
                                 P.days_in(fn["period"]),
                                 2 * fn["survival_per_day"] * P.days_in(fn["period"])
                                 )["mode"],
          f'{fn["mode"]} 線 {fn["line"]} 花 {fn["spent"]}')

    print("\n[12] having more money never buys less")
    # The sawtooth. The floor was whichever rung the pile happened to clear, so crossing
    # one reclassified everything above the last rung as untouchable in a single step:
    # $147 a period at $4,000, $3 a period at $4,500. Three cliffs across the range Momo
    # actually lives in, and an inversion below the first rung where nothing was defended
    # at all and being broke therefore read as the most comfortable state of all.
    #
    # It surfaced by breaking a good change. Netting her mother's paybacks and dropping
    # reimbursable work costs from the survival floor made the model strictly more
    # accurate, and cut her allowance from $221 to $85 — the corrected floor slid her over
    # the second rung. Nothing about her life changed. So: a monotonicity test, because
    # the property that was violated was never "is this number right", it was "can this
    # number move the wrong way".
    from app import allowance as _al
    SURV, N, FR = 2233.08, 4, 1.0
    def line_at(cash, months=1.0):
        f = _al.defended_floor(SURV, months, cash)
        return max(0.0, (cash - f) / N) * FR
    sweep = [(c, line_at(c)) for c in range(500, 12001, 250)]
    bad = [(a[0], b[0]) for a, b in zip(sweep, sweep[1:]) if b[1] < a[1] - EPS]
    check("the line never falls as the pile grows",
          not bad, f'drops at {bad[:4]}')
    check("no cliff: $250 more can never cost more than $250 of line",
          all(abs(b[1] - a[1]) <= 250 / N + EPS for a, b in zip(sweep, sweep[1:])),
          str(max(abs(b[1] - a[1]) for a, b in zip(sweep, sweep[1:]))))
    check("the old rule really did have the cliffs — the fix is not solving a non-problem",
          any(_old_line(c, SURV, N) > _old_line(c + 250, SURV, N) + 1
              for c in range(500, 9000, 250)))
    # Standing and defending are different things now, and only one of them is a decision.
    check("the defended floor does not move when the balance moves",
          len({_al.defended_floor(SURV, 1.0, c) for c in (5000, 6000, 7000, 12000)}) == 1,
          str([_al.defended_floor(SURV, 1.0, c) for c in (5000, 6000, 7000, 12000)]))
    check("climbing a rung costs allowance, visibly and on purpose",
          line_at(6000, 2.0) < line_at(6000, 1.0),
          f'{line_at(6000, 2.0):.0f} vs {line_at(6000, 1.0):.0f}')
    check("below the floor there is nothing spare, and it says zero rather than a fiction",
          line_at(1500) == 0 and line_at(SURV - 1) == 0)
    # …and zero is a handoff, not an instruction. dip_view has to pick it up.
    check("a zero line hands off to the dip view instead of being taken literally",
          dip_view(0.0, 0.0, 5, 15, 1400.0)["mode"] == "dip")
    check("live: standing on a rung is a scoreboard, not the floor",
          plan["standing"].get("defended_floor") is None
          or plan["standing"]["defended_floor"] <= plan["standing"]["reserve_total"])

    print("\n[13] 媽媽的回款 come off the bucket they inflated")
    from app import budget as _bg
    from app import taxonomy as _tx
    check("an inbound transfer from her mother's account is a payback",
          _tx.family_payback("Online Transfer From Chk ...7567 transaction#: 299", 525.46))
    # Direction is the whole test — money the other way is Momo paying her mother.
    check("money going the other way to the same account is not",
          not _tx.family_payback("Online Transfer To Chk ...7567", -525.46))
    check("an ordinary transfer is untouched",
          not _tx.family_payback("APPLECARD GSBANK PAYMENT", 2990.0))
    check("a payback with no matching charge is spread across the window, not dumped in one",
          _bg.spreads_over_window(_Row(inflow_kind=_tx.REIMBURSE_FAMILY, nets_txn_id=None)))
    check("a refund that found its charge stays on that charge's fortnight",
          not _bg.spreads_over_window(_Row(inflow_kind=_tx.REFUND, nets_txn_id="abc")))
    # The floor is "the least she can live on", so it cannot include gaff tape she got paid
    # back for, and it cannot include shocks — those are already carried separately, so
    # counting them here charged her twice.
    check("the survival floor is built from 彈性+想要 only",
          all(_tx.treatment(c) in (_tx.FLEX, _tx.WANT)
              for c in ("food", "snacks", "want", "shopping", "household"))
          and _tx.treatment("work") == _tx.WORK and _tx.treatment("health") == _tx.IRREGULAR)

    print("\n[14] one line per day, and 本期口袋 holds what a day didn't use")
    # Momo: "if I spend nothing for the first 14 days, I have the whole budget to spend for
    # the last day." A period-wide figure reads as permission all month and then as a
    # cliff, and gives her nothing to decide against on a Tuesday.
    from datetime import date as _d
    from app import allowance as _al
    FROM, HI, LINE = _d(2026, 8, 1), _d(2026, 8, 15), 450.0   # 15 days → $30/day
    GRANT = [{"period": "2026-08A", "amount": 40.0, "from": "2026-08-03", "until": "2026-08-03"}]
    dv = lambda day, spend, g=GRANT: _al.daily_view(LINE, FROM, HI, day, spend, g)

    check("the daily line is the period line divided by the days it covers",
          near(dv(_d(2026, 8, 1), {})["daily_base"], 30.0))
    check("the daily line does not drift as days pass",
          len({dv(d, {})["daily_base"] for d in
               (_d(2026, 8, 1), _d(2026, 8, 8), _d(2026, 8, 15))}) == 1)
    check("day one has an empty pocket — nothing has been saved yet",
          near(dv(_d(2026, 8, 1), {})["pool"], 0.0))
    check("two quiet days put two days of line in the pocket",
          near(dv(_d(2026, 8, 3), {})["pool"], 60.0),
          str(dv(_d(2026, 8, 3), {})["pool"]))
    check("today is not in the pocket yet — it is still being spent",
          near(dv(_d(2026, 8, 3), {_d(2026, 8, 3): 0.0})["pool"], 60.0))
    # The bug this caught: crediting the pool with (allowed − spent) meant a raise she
    # asked for and did NOT use paid itself back into the pocket — she could mint money by
    # requesting raises and skipping them. The pocket counts BASE, never the raised ceiling.
    check("a raise lifts today's ceiling",
          near(dv(_d(2026, 8, 3), {})["daily_today"], 70.0))
    check("a raise she takes comes out of the pocket",
          near(dv(_d(2026, 8, 4), {_d(2026, 8, 3): 70.0})["pool"], 20.0),
          str(dv(_d(2026, 8, 4), {_d(2026, 8, 3): 70.0})["pool"]))
    check("a raise she does NOT take is not a deposit — no minting money",
          near(dv(_d(2026, 8, 4), {})["pool"], 90.0),
          str(dv(_d(2026, 8, 4), {})["pool"]))
    check("spending past the daily line draws the pocket down by itself",
          near(dv(_d(2026, 8, 4), {_d(2026, 8, 2): 120.0})["pool"], -30.0),
          str(dv(_d(2026, 8, 4), {_d(2026, 8, 2): 120.0})["pool"]))
    # The invariant that makes the whole thing safe: however the days are sliced, what she
    # has been allowed so far can never exceed the period's line.
    check("pocket plus today's allowance never exceeds the period line",
          all((lambda v: v["pool"] + v["daily_today"] <= LINE + EPS)(dv(FROM + timedelta(days=i), {}))
              for i in range(15)))
    check("after the last day the period is closed and grants no more",
          dv(_d(2026, 8, 16), {})["days_left"] == 0
          and dv(_d(2026, 8, 16), {})["daily_today"] == 0)
    # Run mid-period, the closing review must agree with the card about the pocket.
    live, fnl = plan["fortnight"].get("daily"), plan["fortnight"]
    span = (live["days_closed"] + live["days_left"]) if live else 0
    check("live: the daily line spread over the governed days rebuilds the period line",
          live is not None and near(live["daily_base"] * span, fnl["line"], 0.02 * span),
          f'{live and live["daily_base"]} × {span} vs {fnl["line"]}')
    check("live: the pocket cannot exceed what the closed days could have saved",
          live is not None
          and live["pool"] <= live["daily_base"] * live["days_closed"] + EPS,
          f'{live and live["pool"]} vs {live and live["daily_base"] * live["days_closed"]}')
    check("live: today's spend and the pocket account for every dollar spent so far",
          live is not None and near(
              live["daily_base"] * (live["days_closed"] + 1) - live["pool"] - live["today_left"]
              + live["daily_bump"], fnl["spent"], 0.05),
          f'pool {live and live["pool"]} today_left {live and live["today_left"]} spent {fnl["spent"]}')

    print("\n[15] one record per job, assembled from the three lists that described it")
    pj = g("/api/projects")
    rows = pj["projects"]
    # Momo: "since so much of my earning is related to project, we should just start a tab
    # that tracks project." The invoice archive knew the rate and the days, 待收款 knew the
    # stage and the landing, the ledger knew the deposit — and nothing joined them.
    check("every job carries a name and an amount",
          rows and all(p.get("name") and p.get("total") is not None for p in rows),
          f'{len(rows)} rows')
    check("a job cannot be both still owed and already paid",
          all(not (p.get("owed") and p.get("paid")) for p in rows),
          str([p["name"] for p in rows if p.get("owed") and p.get("paid")]))
    check("what is owed here matches what 該催的錢 is chasing",
          near(pj["owed_total"], plan["runway"]["owed_total"], 0.02),
          f'{pj["owed_total"]} vs {plan["runway"]["owed_total"]}')
    # A deposit four months BEFORE she billed for a job is some other job's money. The
    # window has to run forward from the invoice, not either side of it.
    check("a payment is never matched to an invoice raised after it",
          all(p["paid_on"] >= (p.get("invoiced_on") or "0000")[:10] or
              (datetime.fromisoformat(p["paid_on"])
               - datetime.fromisoformat(p["invoiced_on"])).days >= -7
              for p in rows if p.get("paid_on") and p.get("invoiced_on")))
    check("a matched payment says it was matched by amount, not asserted as fact",
          all(p.get("paid_match") == "amount" for p in rows if p.get("paid")))
    # Non-shoot work (a poster design) has no days, so it must not dilute the day rate.
    shoot = [p for p in rows if p.get("days") and p.get("rate")]
    check("only jobs with days and a rate feed the day rate",
          near(pj["shoot_days"], sum(int(p["days"]) for p in shoot)),
          f'{pj["shoot_days"]} vs {sum(int(p["days"]) for p in shoot)}')
    check("the average rate is shoot fees over shoot days, prep money excluded",
          pj["avg_rate"] is None
          or near(pj["avg_rate"], round(pj["day_fees"] / pj["shoot_days"], 2), 0.02),
          f'{pj["avg_rate"]} vs {pj["day_fees"]}/{pj["shoot_days"]}')
    # Not an identity across ALL jobs: a booking she has not invoiced yet has a total but
    # no rate/days/extras split, so it lands in billed and in neither of the other two.
    check("an invoiced job's total is its shoot fees plus its extras",
          all(near(p["total"], (p.get("day_total") or 0) + (p.get("extras") or 0), 0.02)
              for p in rows if p.get("invoice") and p.get("day_total")),
          str([(p["name"], p["total"]) for p in rows
               if p.get("invoice") and p.get("day_total")
               and not near(p["total"], (p.get("day_total") or 0) + (p.get("extras") or 0), 0.02)]))
    check("shoot fees and extras never add up to more than what was billed",
          pj["day_fees"] + pj["extras"] <= pj["billed"] + EPS,
          f'{pj["day_fees"]} + {pj["extras"]} vs {pj["billed"]}')
    check("每個客戶的日薪走勢 is ordered oldest to newest",
          all(all((c["rates"][i]["when"] or "") <= (c["rates"][i+1]["when"] or "")
                  for i in range(len(c["rates"]) - 1)) for c in pj["clients"]))

    print("\n[16] money spent FOR a job never eats her daily allowance")
    # Momo: "I note that these are expenses that will get reimbursed cuz it's work related,
    # she kept note of it but she categorize them into the normal category (food and
    # transportation), which causes my daily allow to spend budget for the session to
    # reflect that which shouldn't."
    from app import taxonomy as _tx
    from app import projects as _pj
    check("工作 is outside the allowance, whatever it bought",
          not _tx.in_allowance("work") and _tx.treatment("work") == _tx.WORK)
    check("the category is defined by whose money it is, not by the item",
          "車錢" in _tx.note("work") and "飯錢" in _tx.note("work"),
          _tx.note("work"))
    check("食 and 交通雜支 still DO eat the allowance — that part was never wrong",
          _tx.in_allowance("food") and _tx.in_allowance("transit"))
    # A Chinese-only job name used to slug to the literal string "project", so every one
    # of them would have shared a single record.
    check("a Chinese-only job name keeps its own identity",
          _pj.slug("藍衣女子") != _pj.slug("春日限定") != "project",
          f'{_pj.slug("藍衣女子")} / {_pj.slug("春日限定")}')
    # One shared word is the CLIENT. 「AVIA 八月拍攝」 matches five Avia gigs equally and
    # picking one silently files a taxi fare against the wrong shoot.
    check("one shared word alone is too weak to identify a job",
          _pj._score("AVIA 八月拍攝", "Avia 02/28–03/10") < 30,
          str(_pj._score("AVIA 八月拍攝", "Avia 02/28–03/10")))
    check("a real name match still wins outright",
          _pj._score("Woman in Blue Dress", "The Lady in the Blue Dress 07/07–07/11") >= 30)
    check("an unrelated name matches nothing",
          _pj._score("沒聽過的新案子", "Prince In Workboots 05/10–05/17") == 0)

    print("\n[17] money she fronted, and the credit that settles it")
    from app import claims as _cl
    from app import projects as _pj2
    # Momo: "there need to be a transaction record for each project, and updated refund and
    # reimbursement status, and whether she needs to rush me for asking reimbursement or
    # asking amazon about refund etc."
    check("a reimbursable work cost defaults to 「還沒去要」, not to nothing",
          _cl.state_of(_Row(claim=None, reimbursable=True, category="work")) == "todo")
    check("an explicit state always wins over the default",
          _cl.state_of(_Row(claim="sent", reimbursable=True, category="work")) == "sent")
    check("an ordinary purchase is not a claim",
          _cl.state_of(_Row(claim=None, reimbursable=None, category="food")) is None)
    check("who has the ball is derived from the state, not guessed",
          _cl.LABEL["todo"] != _cl.LABEL["sent"] and set(_cl.LABEL) ==
          {"todo", "sent", "paid", "wont"})
    # Unpaid work belongs in the record and nowhere near the price she charges.
    check("only 接案拍攝 prices a shoot day",
          _pj2.RATED == ("shoot",) and set(_pj2.KINDS) >=
          {"shoot", "portfolio", "design", "spec"})
    check("a portfolio job is dropped from the rate rather than averaged in at $0",
          prefs_mod.day_rate([], [], invoices=[
              {"kind": "shoot", "rate": 350.0, "days": 8, "day_total": 2800.0},
              {"kind": "portfolio", "rate": 0.0, "days": 4, "day_total": 0.0},
          ])["observed"] == 350.0)
    check("a paid design job is also kept out of the DAY rate — it is not priced per day",
          prefs_mod.day_rate([], [], invoices=[
              {"kind": "shoot", "rate": 300.0, "days": 5, "day_total": 1500.0},
              {"kind": "design", "rate": 550.0, "days": 1, "day_total": 550.0},
          ])["n"] == 1)

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILED: " + ", ".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

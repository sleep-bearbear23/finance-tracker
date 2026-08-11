"""Can she actually change the data — and can it be taken back?

Momo's test: 「我九月的 Avia 檔期確定了 9/6-9/15，拍八天，總共大概也是$2800的收入」. The old
router never reached a write path and she said 「幫你加一筆待收款」 anyway. These check the
three things that failure was made of:

  1. every write is a real tool with a real effect
  2. a tool that fails says so, and leaves nothing behind
  3. everything that landed can be undone from what was logged

    PYTHONPATH=. python3 tests_tools.py
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

os.environ.update(
    DATABASE_URL="sqlite+aiosqlite:////tmp/tools.db",
    ANTHROPIC_API_KEY="x", LINE_CHANNEL_ACCESS_TOKEN="x", LINE_CHANNEL_SECRET="x",
    DASHBOARD_TOKEN="tok",
)
Path("/tmp/tools.db").unlink(missing_ok=True)

from sqlalchemy import func, select  # noqa: E402

from app import agent, changelog, fixed, migrate, prefs, tools  # noqa: E402
from app.db import Session, engine, get_kv, init_db  # noqa: E402
from app.models import Change, MerchantMemory, Transaction  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{('  — ' + detail) if detail else ''}")


def near(a, b, eps=0.011):
    return abs((a or 0) - (b or 0)) < eps


async def n_changes(s):
    return await s.scalar(select(func.count(Change.id)))


async def main():
    await init_db()
    await migrate.run(engine)

    async with Session() as s:
        print("\n[1] the message that started this — a booking, in her own words")
        before = await n_changes(s)
        out = await tools.run(s, "add_expected_payment", {
            "amount": 2800, "note": "Avia 九月檔期 9/6-9/15，拍八天", "when": "2026-09"},
            source_text="我九月的 Avia 檔期確定了 9/6-9/15，拍八天，總共大概也是$2800的收入")
        check("the booking is written", out["ok"], out.get("error", ""))
        pend = await prefs.pending_invoices(s)
        check("it shows up as a pending payment",
              any(near(p["amount"], 2800) for p in pend), str(pend))
        check("the summary says what it did, with the number",
              "2,800" in out["summary"] and "2026-09" in out["summary"], out["summary"])
        check("it logged exactly one change", await n_changes(s) == before + 1)

        print("\n[2] a tool that cannot find its target fails loudly and writes nothing")
        before = await n_changes(s)
        miss = await tools.run(s, "update_expected_payment",
                               {"which": "沒有這個案子", "amount": 999})
        check("a miss returns ok=false", not miss["ok"], str(miss)[:90])
        check("a miss says what it does have", bool(miss.get("have")), str(miss.get("have"))[:70])
        check("a miss leaves no change row", await n_changes(s) == before)

        print("\n[3] before → after, on the number Momo actually watches")
        up = await tools.run(s, "update_expected_payment",
                             {"which": "Avia 九月", "amount": 2850})
        check("the update lands", up["ok"], str(up)[:80])
        check("the summary carries both sides",
              "2,800" in up["summary"] and "2,850" in up["summary"], up["summary"])

        print("\n[4] fixed costs — the write path that did not exist at all")
        base_rows = await fixed.rows(s, include_sinking=False)
        base_total = await fixed.monthly_total(s)
        add = await tools.run(s, "add_fixed_cost", {
            "name": "Anthropic API（機器人）", "amount": 40, "cadence": "monthly",
            "category": "subs", "where": "Apple Card"})
        check("a subscription can be added", add["ok"], str(add)[:80])
        rows_now = await fixed.rows(s, include_sinking=False)
        check("adding one row does not wipe the other nine",
              len(rows_now) == len(base_rows) + 1, f"{len(base_rows)} → {len(rows_now)}")
        check("the monthly total moved by exactly the new cost",
              near(await fixed.monthly_total(s), base_total + 40),
              f"{base_total} → {await fixed.monthly_total(s)}")

        semi = await tools.run(s, "add_fixed_cost", {
            "name": "測試半年費", "amount": 600, "cadence": "semiannual"})
        check("a semiannual bill is divided, not counted whole",
              near(await fixed.monthly_total(s), base_total + 40 + 100),
              str(await fixed.monthly_total(s)))
        check("and the summary shows the per-month translation",
              "100" in semi["summary"], semi["summary"])

        print("\n[4b] the double-charge trap")
        dup = await tools.run(s, "add_fixed_cost", {
            "name": "Claude 訂閱", "amount": 100, "cadence": "monthly"})
        check("a second line for a bill she already has is refused",
              not dup["ok"] and "Claude" in dup["error"], str(dup)[:110])
        check("…and the refusal names the row it collided with",
              bool(dup.get("duplicate")), str(dup.get("duplicate")))
        check("the total did not move on a refused add",
              near(await fixed.monthly_total(s), base_total + 40 + 100),
              str(await fixed.monthly_total(s)))
        forced = await tools.run(s, "add_fixed_cost", {
            "name": "Claude 訂閱", "amount": 100, "cadence": "monthly", "force": True})
        check("but she can still say 'no really, both'", forced["ok"])
        # now two rows contain "Claude" — deleting on a partial name must not pick one
        amb = await tools.run(s, "remove_fixed_cost", {"which": "Claude"})
        check("an ambiguous name is asked about, not guessed at",
              not amb["ok"] and len(amb.get("candidates") or []) == 2, str(amb)[:120])
        gone = await tools.run(s, "remove_fixed_cost", {"which": "Claude 訂閱"})
        check("an exact name still resolves, and to the exact row",
              gone["ok"] and "含加值" not in gone["summary"], gone.get("summary") or str(gone)[:90])

        dup2 = await tools.run(s, "add_expected_payment", {
            "amount": 2850, "note": "Avia 九月檔期", "when": "2026-09"})
        check("the same booking twice is refused too",
              not dup2["ok"] and bool(dup2.get("duplicate")), str(dup2)[:110])

        chg = await tools.run(s, "update_fixed_cost",
                              {"which": "Anthropic", "amount": 55})
        check("a price change lands", chg["ok"] and near(await fixed.monthly_total(s),
                                                         base_total + 55 + 100))
        rm = await tools.run(s, "remove_fixed_cost", {"which": "測試半年費"})
        check("a cancelled subscription can be removed",
              rm["ok"] and near(await fixed.monthly_total(s), base_total + 55))

        print("\n[5] balances, and the totals that hang off them")
        bal = await tools.run(s, "set_account_balance",
                              {"name": "Apple Card", "amount": 2626.01, "kind": "credit"})
        check("a new account can be set from chat", bal["ok"], str(bal)[:80])
        bal2 = await tools.run(s, "set_account_balance",
                               {"name": "Apple Card", "amount": 2700, "kind": "credit"})
        check("a second report reads as a change, not a new account",
              "2,626.01" in bal2["summary"] and "2,700" in bal2["summary"], bal2["summary"])
        check("the debt total follows the account",
              near(float(await get_kv(s, "cfg_total_debt")), 2700),
              await get_kv(s, "cfg_total_debt"))

        print("\n[6] the plan itself")
        sav = await tools.run(s, "set_savings_plan", {"amount": 500, "cadence": "biweekly"})
        p = await prefs.get_prefs(s)
        check("savings target and cadence both land",
              sav["ok"] and near(p["savings_amount"], 500) and p["savings_cadence"] == "biweekly",
              str(p))
        em = await tools.run(s, "set_emergency_target", {"amount": 20000})
        check("the emergency target can be pinned",
              em["ok"] and near(float(await get_kv(s, "cfg_emergency_target")), 20000))
        un = await tools.run(s, "set_emergency_target", {"amount": 0})
        check("and unpinned back to the computed one",
              "自動計算" in un["summary"], un["summary"])

        print("\n[7] cash spending the bank will never see")
        exp = await tools.run(s, "log_expense",
                              {"amount": 24, "merchant": "路邊停車", "category": "car"})
        check("a cash expense is recorded", exp["ok"], str(exp)[:80])
        t = await s.get(Transaction, exp["id"])
        check("it is stored as a spend, not income", t is not None and t.amount < 0,
              str(t.amount if t else None))

        print("\n[8] undo — the condition Momo attached to full authority")
        log = await changelog.recent(s, 50)
        check("every write is in the log", len(log) >= 10, str(len(log)))
        check("each entry says what it did in Chinese",
              all(e["summary"] for e in log), str([e["tool"] for e in log if not e["summary"]]))

        exp_change = next(e for e in log if e["tool"] == "log_expense")
        r = await changelog.undo(s, exp_change["id"])
        check("undoing a logged expense reports ok", r["ok"], str(r))
        check("…and the transaction is gone",
              await s.get(Transaction, exp["id"]) is None)
        again = await changelog.undo(s, exp_change["id"])
        check("undoing twice is refused, not silently repeated", not again["ok"], str(again))

        add_change = next(e for e in log if e["tool"] == "add_fixed_cost"
                          and "Anthropic" in e["summary"])
        # the price change came after; undoing the add must not resurrect the old price
        await changelog.undo(s, add_change["id"])
        names = [r.get("name") for r in await fixed.rows(s, include_sinking=False)]
        check("undoing an add removes the row it added",
              not any("Anthropic" in (n or "") for n in names), str(names))

        bal_change = next(e for e in log if e["tool"] == "set_account_balance"
                          and "2,700" in e["summary"])
        await changelog.undo(s, bal_change["id"])
        accts = prefs._load_list(await get_kv(s, "cfg_accounts"))
        card = next((a for a in accts if "Apple" in (a.get("name") or "")), None)
        check("undoing a balance puts the old number back",
              card is not None and near(card["amount"], 2626.01), str(card))
        check("and the derived total follows it back",
              near(float(await get_kv(s, "cfg_total_debt")), 2626.01),
              await get_kv(s, "cfg_total_debt"))

        print("\n[9] setting something to what it already was is not an event")
        before = await n_changes(s)
        await tools.run(s, "set_savings_plan", {"amount": 500, "cadence": "biweekly"})
        check("a no-op write leaves the log alone", await n_changes(s) == before,
              f"{before} → {await n_changes(s)}")

        print("\n[10] the loop: she speaks after the tools, never before")

        class _Blocks(list):
            pass

        class _Resp:
            def __init__(self, content):
                self.content = content

        class _Text:
            type = "text"

            def __init__(self, t):
                self.text = t

        class _Use:
            type = "tool_use"

            def __init__(self, name, inp, id_="tu_1"):
                self.name, self.input, self.id = name, inp, id_

        class _Fake:
            """Stands in for the API: first turn calls a tool, second turn talks."""

            def __init__(self, script):
                self.script, self.seen = list(script), []
                self.messages = self

            async def create(self, **kw):
                self.seen.append(kw)
                return self.script.pop(0)

        real = agent._client
        agent._client = _Fake([
            _Resp([_Use("add_expected_payment",
                        {"amount": 1200, "note": "測試案子", "when": "2026-10"})]),
            _Resp([_Text("好，記起來了。")]),
        ])
        res = await agent.handle(s, "十月接了一個案子，$1200")
        agent._client = real
        check("the tool actually ran inside the loop",
              res["calls"] and res["calls"][0]["result"]["ok"], str(res["calls"])[:100])
        check("her reply is generated after the result, not before",
              "記起來了" in res["reply"])
        check("the receipts are attached to what she says",
              "1,200" in agent.compose(res) and "改好了" in agent.compose(res),
              agent.compose(res)[-90:])
        check("the receipt line comes from the tool, not the model",
              res["changes"] == [res["calls"][0]["result"]["summary"]])

        agent._client = _Fake([_Resp([_Text("這個我幫你加好了喔。")])])
        res2 = await agent.handle(s, "隨便講一句")
        agent._client = real
        check("a claim with no tool call gets no receipts",
              res2["changes"] == [] and "改好了" not in agent.compose(res2),
              agent.compose(res2))

        print("\n[11] filing the charges she asked about is a tool now, not a keyword gate")
        from app import enrichment, llm  # noqa: PLC0415
        from app.config import now as _now  # noqa: PLC0415
        for i, (amt, who) in enumerate(((-18.5, "BLUE BOTTLE"), (-62.0, "SHELL OIL")), 1):
            s.add(Transaction(id=f"pend{i}", account_id="applecard", amount=amt,
                              merchant_desc=who, posted_at=_now(), status="prompted",
                              batch_id="b1", batch_seq=i, prompted_at=_now(), source="applecard"))
        await s.commit()

        real_parse = llm.parse_reply

        async def _no_match(batch, reply):
            return {}
        llm.parse_reply = _no_match
        off = await tools.run(s, "answer_pending_charges", {"reply": "阿姨你今天好嗎"})
        llm.parse_reply = real_parse
        check("a reply that maps to nothing does not close the batch", not off["ok"], str(off)[:80])
        check("…and the charges are still waiting",
              len(await enrichment.pending_batch(s)) == 2)

        async def _match(batch, reply):
            return {1: {"note": "咖啡", "category": "snacks", "inflow": None},
                    2: {"note": "加油", "category": "gas", "inflow": None}}
        llm.parse_reply = _match
        ok = await tools.run(s, "answer_pending_charges",
                             {"reply": "1 是咖啡 2 是加油"}, source_text="1 是咖啡 2 是加油")
        llm.parse_reply = real_parse
        check("a real reply files them", ok["ok"], str(ok)[:90])
        check("…the receipt names what was filed",
              "咖啡" in ok["summary"] or "零食" in ok["summary"], ok["summary"])
        check("…and nothing is left waiting", not await enrichment.pending_batch(s))

        log2 = await changelog.recent(s, 5)
        enr = next(c for c in log2 if c["tool"] == "answer_pending_charges")
        await changelog.undo(s, enr["id"])
        t1 = await s.get(Transaction, "pend1")
        check("undo puts the charges back on the waiting list",
              t1.status == "prompted" and t1.category is None,
              f"{t1.status} / {t1.category}")

        check("the tools were offered to the model at all",
              len(tools.SCHEMAS) >= 12 and all("input_schema" in t for t in tools.SCHEMAS),
              str(len(tools.SCHEMAS)))
        check("every advertised tool has a handler",
              set(tools.NAMES) == set(tools.HANDLERS),
              str(set(tools.NAMES) ^ set(tools.HANDLERS)))

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILED: " + ", ".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

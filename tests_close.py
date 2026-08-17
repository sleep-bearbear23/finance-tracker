"""期末結算 — Batch A tests. The boundary, the freeze, the grace, the backup stamp.

    python3 tests_close.py
"""
from __future__ import annotations

import asyncio
import json
import os
from datetime import date, timedelta

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("ANTHROPIC_API_KEY", "x")
os.environ.setdefault("LINE_CHANNEL_ACCESS_TOKEN", "x")
os.environ.setdefault("LINE_CHANNEL_SECRET", "x")

from sqlalchemy import select  # noqa: E402

from app import allowance, budget, migrate, settle  # noqa: E402
from app import period as P  # noqa: E402
from app.config import now  # noqa: E402
from app.db import Session, engine, get_kv, init_db, set_kv  # noqa: E402
from app.models import Settlement, Transaction  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{('  — ' + detail) if detail else ''}")


async def main() -> int:
    await init_db()
    await migrate.run(engine)

    async with Session() as s:
        cur = budget.current_key()
        prev = P.prev_key(cur)
        older = P.prev_key(prev)

        print("\n[1] the grace point — nothing freezes retroactively")
        st = await settle.state(s)
        check("a fresh install owes no settlement at all",
              st["awaiting"] is False, str(st))
        check("…and it stamped where the requirement begins",
              await get_kv(s, settle.FROM_KEY) == cur)

        print("\n[2] once a period ends unsettled, the budget waits")
        await set_kv(s, settle.FROM_KEY, older)     # pretend we launched two periods ago
        st = await settle.state(s)
        check("both ended periods are owed a meeting",
              st["awaiting"] is True and st["periods"] == [older, prev], str(st["periods"]))
        check("the oldest one is the one she's asked about first", st["oldest"] == older)
        check("the period we're standing in is never owed — it hasn't ended",
              cur not in st["periods"])

        print("\n[3] the engine says so, and says it in words")
        a = await allowance.compute(s)
        check("compute carries the flag", bool(a.get("awaiting_settlement")))
        lines = allowance.explain(a)
        check("explain refuses to quote a budget, and says why",
              len(lines) == 1 and "還沒結算" in lines[0] and "先不給你數字" in lines[0],
              lines[0][:50])
        check("…and promises the記帳 keeps working", "帳我照記" in lines[0])

        print("\n[4] settling clears it, one period at a time")
        await settle.record(s, older, pocket=120.0, destination="quarter",
                            reflection={"worth": "買了新的燈"})
        st = await settle.state(s)
        check("the settled period drops out, the next stays owed",
              st["awaiting"] is True and st["periods"] == [prev], str(st["periods"]))
        await settle.record(s, prev, pocket=0.0, destination="carry")
        st = await settle.state(s)
        check("both settled → the budget is free again", st["awaiting"] is False)
        a = await allowance.compute(s)
        check("…and the engine stops flagging it", a.get("awaiting_settlement") is None)

        print("\n[5] one row per period, ever")
        before = await settle.get_one(s, older)
        again = await settle.record(s, older, pocket=999.0, destination="jar")
        check("a second submit returns the first row instead of double-crediting",
              again.id == before.id and abs(again.pocket - 120.0) < 0.01,
              f"{again.pocket}")

        print("\n[6] her own words come back next time")
        last = await settle.last_reflection(s)
        check("the most recent non-empty reflection is found",
              last is not None and last["answers"].get("worth") == "買了新的燈", str(last))
        check("…and a skipped reflection is skipped, not returned as empty",
              (await settle.last_reflection(s, before_key=older)) is None)

        print("\n[7] the generated question is earned, never invented")
        q = await settle.noticed_question(s, cur)
        check("an empty period produces no question at all", q is None, str(q))
        lo, _ = P.key_bounds(cur)
        for i in range(9):
            s.add(Transaction(id=f"eat-{i}", account_id="chase", amount=-18.0,
                              merchant_desc="UBER EATS", category="food",
                              status="enriched",
                              posted_at=now().replace(hour=12) - timedelta(days=0)))
        await s.commit()
        q = await settle.noticed_question(s, cur)
        check("nine deliveries earns the 外食 question",
              q is not None and "外食" in q and "9" in q, str(q))

        print("\n[8] backup — measured, not guessed")
        bs = await settle.backup_state(s)
        check("never exported reads as stale", bs["stale"] is True and bs["last"] is None)
        await settle.stamp_export(s)
        bs = await settle.backup_state(s)
        check("after an export it's fresh, and dated",
              bs["stale"] is False and bs["days"] == 0 and bs["last"], str(bs))
        msg = settle.backup_message(bs, "https://example.up.railway.app")
        check("the reminder names the folder and the link every time",
              settle.BACKUP_FOLDER in msg and "/api/export" in msg, msg[:60])
        await set_kv(s, settle.EXPORT_KEY, (now().date() - timedelta(days=9)).isoformat())
        bs = await settle.backup_state(s)
        check("nine days later it's due again", bs["stale"] is True and bs["days"] == 9)

        print("\n[9] the two messages")
        clo = await allowance.closure(s, prev)
        note = settle.close_notice(clo, "https://x.test")
        check("the close notice carries the link and promises記帳 continues",
              "/settle" in note and "帳還是照記" in note, note[:60])
        om = await settle.open_message(s, cur, {"target": 9000.0, "secured": 6000.0,
                                                "days_needed": 8.6})
        check("the open message leads with this period's line",
              om.startswith(P.label(cur)) and "這期的線" in om, om[:40])
        check("…and hands the quarter gap over as shoot days",
              "還差" in om and "8" in om, om)

    async with Session() as s:
        print("\n[10] the quarter — bounds, grace, and one row")
        from app import settle as ST
        q = ST.prev_quarter(date(2026, 9, 5))
        check("standing in Q4, the quarter owed is Q3 ending 8/31",
              q is not None and q[1].isoformat() == "2026-08-31"
              and q[2] == "Q:2026-08-31", str(q))
        # Before the tax table starts there is no known previous season. Inventing one
        # would demand a settlement for a quarter nobody has records of, so None is the
        # honest answer and the ritual simply doesn't fire.
        q_mid = ST.prev_quarter(date(2026, 7, 15))
        check("no known previous season → nothing is demanded", q_mid is None, str(q_mid))

        print("\n[11] the proposal fills the jars that are furthest from doing their job")
        js = [
            {"id": "contingency", "name": "短期應急", "balance": 0.0, "target": 600.0},
            {"id": "dmv", "name": "DMV", "balance": 15.0, "target": 371.0},
            {"id": "car", "name": "修車", "balance": 50.0, "target": 1200.0},
            {"id": "floor", "name": "地板", "balance": 2233.0, "target": 2233.0},
            {"id": "emergency", "name": "緊急預備金", "balance": 3010.0, "target": 13250.0},
        ]
        pr = ST.propose(1000.0, js)
        by = {p["id"]: p for p in pr}
        check("safety first — the small buffer fills before the big fund",
              by["contingency"]["suggest"] == 600.0, str(by["contingency"]))
        check("a full jar is proposed nothing", by["floor"]["suggest"] == 0.0)
        check("the proposal never exceeds what there is",
              abs(sum(p["suggest"] for p in pr) - 1000.0) < 0.01,
              str(sum(p["suggest"] for p in pr)))
        check("…and the leftover lands on the next jar down, not spread thin",
              by["dmv"]["suggest"] == 356.0 and by["car"]["suggest"] == 44.0,
              f"{by['dmv']['suggest']}/{by['car']['suggest']}")
        check("nothing to give → nothing proposed",
              all(p["suggest"] == 0.0 for p in ST.propose(0.0, js)))

        print("\n[12] a quarter settlement is a settlement like any other")
        await ST.record(s, "Q:2026-08-31", pocket=1000.0, destination="allocated",
                        kind="quarter", allocations={"contingency": 600.0},
                        objective={"type": "chase", "text": "這一季把 $10,600 催回來"})
        qp = await ST.quarter_pending(s, date(2026, 9, 5))
        check("once settled it stops being pending", qp is None, str(qp))
        obj = await ST.current_objective(s)
        check("the objective survives, for the next period to open with",
              obj is not None and "催回來" in obj["text"], str(obj))
        om = await ST.open_message(s, budget.current_key(), None)
        check("…and the session-open message says it out loud",
              "催回來" in om, om)

    async with Session() as s:
        print("\n[13] a jar Momo makes herself")
        from app import jars, tools
        from app import settle as ST
        await jars.seed(s, floor_amount=0.0, gs_balance=0.0, tax_outstanding=0.0,
                        emergency_target=0.0, season_pot=0.0)
        r = await tools.run(s, "jar_create", {"name": "台灣旅費", "target": 2500,
                                              "by_date": "2027-06-01"})
        check("she can open one by talking", r.get("ok") is True, str(r.get("error")))
        check("a CJK name keeps a usable id, not a collision magnet",
              r["jar"]["id"] == "台灣旅費")
        check("a goal jar never drips", r["jar"]["fill"] == "allocate")
        check("…and starts empty, so it changes no number",
              r["jar"]["balance"] == 0.0)
        sf_before = await jars.spoken_for(s, tax_outstanding=0.0)
        check("spoken_for is untouched by an unfunded goal",
              abs(sf_before["total"]) < 0.01, str(sf_before["total"]))
        check("the deadline becomes advice, with the arithmetic shown",
              "每期" in (r.get("advice") or ""), r.get("advice"))

        print("\n[14] she can be addressed by her own name")
        out = await tools.run(s, "jar_allocate", {"jar": "台灣旅費", "amount": 300})
        check("a jar Momo named is reachable in conversation", out.get("ok") is True)
        out = await tools.run(s, "jar_allocate", {"jar": "台灣", "amount": 50})
        check("…and by part of the name", out.get("ok") is True)
        sf_after = await jars.spoken_for(s, tax_outstanding=0.0)
        check("only money actually moved is spoken for",
              abs(sf_after["total"] - 350.0) < 0.01, str(sf_after["total"]))

        print("\n[15] funding a jar is an OBJECTIVE, not an obligation")
        opts = await ST.fund_options(s)
        tw = next((o for o in opts if o["id"] == "台灣旅費"), None)
        check("the jar is offered as a quarter objective", tw is not None)
        js = await jars.load(s)
        fo = ST.fund_objective(jars.get(js, "台灣旅費"), 70)
        check("70% is of the FULL target", abs(fo["target_balance"] - 1750.0) < 0.01,
              str(fo["target_balance"]))
        check("…and the hint says what that means from here",
              abs(fo["add"] - 1400.0) < 0.01 and "已經有" in fo["why"], fo["why"])

        print("\n[16] objectives carry rank, and get graded later")
        objs = [{"type": "chase", "rank": "primary", "amount": 7200.0, "text": "催回 $7,200"},
                {**fo, "rank": "secondary"}]
        await ST.record(s, "Q:2026-05-31", pocket=0.0, destination="allocated",
                        kind="quarter", objective=objs)
        cur = await ST.current_objectives(s)
        check("the primary comes back first", cur and cur[0]["rank"] == "primary", str(len(cur)))
        om = await ST.open_message(s, budget.current_key(), None)
        check("the period opens with the primary, secondaries quietly listed",
              "主目標" in om and "次要的" in om, om)
        scored = await ST.score(s, objs, date(2026, 3, 1), date(2026, 5, 31))
        fund_row = next(o for o in scored if o["type"] == "fund")
        check("a fund objective is graded from the jar itself",
              fund_row["verdict"] == "missed" and "350" in fund_row["detail"],
              fund_row["detail"])
        await jars.allocate(s, "台灣旅費", 1400.0)
        scored = await ST.score(s, objs, date(2026, 3, 1), date(2026, 5, 31))
        fund_row = next(o for o in scored if o["type"] == "fund")
        check("…and reads as done once the money is actually in",
              fund_row["verdict"] == "hit", fund_row["detail"])
        book = await ST.score(s, [{"type": "book", "text": "接 15 天"}],
                              date(2026, 3, 1), date(2026, 5, 31))
        check("what can't be measured says so instead of guessing a grade",
              book[0]["verdict"] == "unknown" and book[0]["detail"], book[0]["detail"])

    async with Session() as s:
        print("\n[17] rehearsal writes nothing")
        from app import settle as ST, tools
        out = await tools.run(s, "start_settlement", {"scope": "quarter"})
        check("asking early gets a preview link, not a false promise",
              out["ok"] and "preview=quarter" in out["reply"] and "不會寫進去" in out["reply"],
              out["reply"][-60:])
        out = await tools.run(s, "start_settlement", {"scope": "session"})
        check("nothing due → a session rehearsal link", "preview=session" in out["reply"])
        # a season that has not ended can never be closed, by any route
        future = ST.quarter_key(date(2099, 1, 1))
        got = await ST.get_one(s, future)
        check("no settlement exists for a future season", got is None)

    async with Session() as s:
        print("\n[18] a rehearsal touches nothing — every mutation guarded, not just the send")
        from app import main as M
        from app import settle as ST
        from app.models import Message
        # far enough back that some periods are genuinely unsettled (earlier sections
        # already settled the two most recent ones)
        far = budget.current_key()
        for _ in range(5):
            far = P.prev_key(far)
        await set_kv(s, ST.FROM_KEY, far)
        st = await ST.state(s)
        owed = st["oldest"]
        check("a period is owed, so the job has something to say", bool(owed), str(st))

        before_notice = await get_kv(s, f"settle_notice:{owed}")
        before_msgs = len((await s.execute(select(Message))).scalars().all())
        res = await M._boundary_job(dry=True)
        after_notice = await get_kv(s, f"settle_notice:{owed}")
        after_msgs = len((await s.execute(select(Message))).scalars().all())

        check("the rehearsal renders the REAL message",
              res["action"] == "session_notice" and "/settle" in (res["text"] or ""),
              str(res.get("action")))
        check("the idempotency key is NOT burned — the real notice still fires later",
              before_notice == after_notice and after_notice != "1",
              f"{before_notice!r}→{after_notice!r}")
        check("conversation memory is not polluted by a rehearsal",
              before_msgs == after_msgs, f"{before_msgs}→{after_msgs}")
        check("…and it says which mode it ran in", res["mode"] == "rehearsal")

        print("\n[19] a rehearsal ignores the once-only caps, so you can run it twice")
        for k in await ST.unsettled(s):
            await ST.record(s, k, pocket=0.0, destination="carry")
        await set_kv(s, f"period_open:{budget.current_key()}", "1")
        res = await M._boundary_job(dry=True)
        # live mode would stop here ("already sent"); a rehearsal is not part of the
        # day's quota, so it renders anyway — that is the point of being able to rehearse.
        check("the already-sent guard does not block a rehearsal",
              res["action"] == "period_open" and res["text"], str(res.get("why")))
        again = await M._boundary_job(dry=True)
        check("…and running it twice is identical, because nothing moved",
              again["text"] == res["text"])
        check("the live guard is still armed underneath",
              await get_kv(s, f"period_open:{budget.current_key()}") == "1")

        print("\n[20] the backup rehearsal shows the words without starting the clock")
        await settle.stamp_export(s)
        before = await get_kv(s, "last_run:backup_nudge")
        res = await M._backup_job(dry=True)
        check("not due → says so, but still shows what it would say",
              res["action"] is None and "備份" in (res["text"] or ""), str(res["why"]))
        check("…and never stamps the nudge", await get_kv(s, "last_run:backup_nudge") == before)
        await set_kv(s, settle.EXPORT_KEY, (now().date() - timedelta(days=20)).isoformat())
        res = await M._backup_job(dry=True)
        check("due → the rehearsal renders the real nudge",
              res["action"] == "backup_nudge" and settle.BACKUP_FOLDER in res["text"])
        check("…and STILL writes nothing",
              await get_kv(s, "last_run:backup_nudge") == before)

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILED: " + ", ".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

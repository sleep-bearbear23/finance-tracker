"""期末結算 — Batch A tests. The boundary, the freeze, the grace, the backup stamp.

    python3 tests_close.py
"""
from __future__ import annotations

import asyncio
import json
import os
from datetime import timedelta

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("ANTHROPIC_API_KEY", "x")
os.environ.setdefault("LINE_CHANNEL_ACCESS_TOKEN", "x")
os.environ.setdefault("LINE_CHANNEL_SECRET", "x")

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

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILED: " + ", ".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

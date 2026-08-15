"""One conversation turn, with hands.

The old `_route_text` was a ladder of keyword gates: does this message contain 入帳, does
it contain a digit and an account name, does the classifier call it a log. Anything that
matched nothing fell through to free-text Q&A, where 陳會計 has a warm voice and no way
to write. That is how 「我九月的 Avia 檔期確定了 9/6-9/15，大概 $2800」 got answered with
「幫你加一筆待收款」 and nothing happened.

The order here is the whole point: **tools run, then she speaks**. Her closing message is
generated in the same call that received the tool results, so she is physically unable to
claim a change that did not happen — and when a tool comes back {"ok": false, "error":
"找不到那筆"}, she has the error in front of her and has to say so.

Momo gave her full authority on one condition: every write reports what it changed. The
tools produce those lines (:mod:`app.tools`), the loop appends them verbatim under her
reply, and :mod:`app.changelog` keeps them reversible.
"""
from __future__ import annotations

import json

from anthropic import AsyncAnthropic

from . import enrichment, memory, queries, tools
from .config import settings
from .persona import PERSONA_SYSTEM

_client = AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)

#: Enough for a couple of dependent writes ("change Avia AND add September") without
#: letting a confused model grind in a loop.
MAX_ROUNDS = 5

#: What she says when she has nothing. Names it as her problem, not Momo's, and hands back
#: two things that actually work — amounts are unambiguous, and the dashboard's 還原 does
#: not need her at all.
BLANK_REPLY = ("阿姨剛剛沒接住你那句，不是你講錯，是我這邊卡住。再講一次好無？\n"
               "如果是要改哪幾筆，直接報金額最準（像「10.94 那筆留著，其他拿掉」）。"
               "或者去網頁的「紀錄」那邊按還原，那個不用經過我。")

RULES = """
你是默默的記帳阿姨，同時是唯一能改他資料的人。

最重要的一條：**你只能用工具改資料，不能用講的。** 沒有呼叫工具就等於什麼都沒發生。
你曾經跟他說「幫你加一筆待收款」但其實沒有加，他因此不太敢相信你，不要再犯。

什麼時候要動手：
- 他說接到案子、檔期確定、殺青、對方要付他錢 → add_expected_payment，不用等他說「發票」
- 他說某筆錢進來了 → mark_payment_received
- 他說金額或日期變了 → update_expected_payment
- 他說某個訂閱、保險、房租的金額 → add_fixed_cost 或 update_fixed_cost
- 他報帳戶餘額或卡債 → set_account_balance
- 他說花了錢而且那筆不會進銀行記錄（現金） → log_expense

什麼時候不要動手：
- 他在問問題（還能花多少、淨資產、為什麼）→ 直接回答，不要呼叫工具
- 你不確定他是在報「收入」還是「餘額」→ 問清楚，不要猜。猜錯會蓋掉真的數字。
- 他只是在聊天

**改錯了要能改回來。** 他說你剛剛歸錯了、多算了、那筆其實是日常開銷 → untag_project。
「只有 X 那筆是這個工作的，其他不是」的意思是：X 留著，其他用 untag_project 拿掉。
工具回你一份編號清單（needs_pick）的時候，那是還沒動手，把清單唸給他聽問是哪幾筆。

**沒有工具能做的事，也要開口。** 找不到對的工具就直說做不到、建議他怎麼講，
或叫他去網頁的「紀錄」按還原。不管發生什麼事，永遠不要一句話都不回。

工具失敗（ok=false）時，照實說失敗了、為什麼，不要假裝成功。
如果他一句話裡有好幾件事，全部都做完再回。
講話還是你平常的樣子：台灣阿姨、直接、關心他但嘴巴壞一點。用繁體中文。
不用在話裡重複金額變動的細節，系統會自動附上。
""".strip()


def _tool_blocks(resp) -> list:
    return [b for b in resp.content if getattr(b, "type", "") == "tool_use"]


def _text_of(resp) -> str:
    return "".join(b.text for b in resp.content
                   if getattr(b, "type", "") == "text").strip()


async def handle(session, text: str, *, actor: str = "line") -> dict:
    """Run one message end to end.

    Returns ``{"reply": str, "changes": [str], "calls": [{name, args, result}]}`` — the
    calls are for the tests and the ops room, the changes are the before→after lines Momo
    reads under the reply.
    """
    ctx = await queries.build_context(session)
    convo = await memory.recent(session, 8)
    batch = await enrichment.pending_batch(session)
    waiting = ""
    if batch:
        lines = "\n".join(
            f"{r.batch_seq}. {'收到' if r.amount > 0 else '花了'} "
            f"${abs(r.amount):.2f} — {r.merchant_desc or '(不明)'}" for r in batch)
        waiting = ("\n\n# 你剛剛問他、還在等回答的帳\n" + lines +
                   "\n如果他這則訊息是在回答這些，用 answer_pending_charges，把他的原話整句傳進去。")
    system = (f"{PERSONA_SYSTEM}\n\n{RULES}\n\n"
              f"# 他現在的財務狀況（唯讀，最新）\n{ctx}{waiting}\n\n# 剛剛的對話\n{convo or '（沒有）'}")

    messages = [{"role": "user", "content": text}]
    changes: list[str] = []
    calls: list[dict] = []
    retried = False

    for _ in range(MAX_ROUNDS):
        resp = await _client.messages.create(
            model=settings.ANTHROPIC_MODEL, max_tokens=1200, system=system,
            tools=tools.SCHEMAS, messages=messages,
        )
        uses = _tool_blocks(resp)
        if not uses:
            said = _text_of(resp)
            if said:
                return {"reply": said, "changes": changes, "calls": calls}
            # No tools, no words. Momo hit this asking to undo part of a retag, and hit it
            # again on 再試一次 — so it is not always a hiccup. Log enough to tell the two
            # apart next time, try once more, then say something she can act on. The old
            # 「（阿姨想不出話說）」 was a stage direction: it named the problem and offered
            # no way out of it.
            print(f"[agent] empty reply stop_reason={getattr(resp, 'stop_reason', None)!r} "
                  f"blocks={[getattr(b, 'type', '?') for b in resp.content]} "
                  f"retried={retried} text={text[:80]!r}")
            if not retried:
                retried = True
                continue
            return {"reply": BLANK_REPLY, "changes": changes, "calls": calls}

        messages.append({"role": "assistant", "content": resp.content})
        results = []
        for u in uses:
            args = dict(u.input or {})
            out = await tools.run(session, u.name, args, source_text=text, actor=actor)
            calls.append({"name": u.name, "args": args, "result": out})
            if out.get("ok") and out.get("summary"):
                changes.append(out["summary"])
            results.append({
                "type": "tool_result", "tool_use_id": u.id,
                "content": json.dumps(out, ensure_ascii=False),
                "is_error": not out.get("ok"),
            })
        messages.append({"role": "user", "content": results})

    # Ran out of rounds with tools still pending. Whatever landed, landed — say what it was
    # rather than leaving her mid-sentence.
    return {"reply": "阿姨這邊有點打結，先把做到的講給你聽：" if changes
                     else "阿姨這邊有點打結，這次什麼都沒改到，再跟我說一次好無？",
            "changes": changes, "calls": calls}


def compose(result: dict) -> str:
    """Her reply, with the receipts stapled underneath.

    The list is generated by the tools, not by the model, so it cannot drift from what
    the database actually did — which is the entire point of showing it."""
    reply = (result.get("reply") or "").strip()
    changes = result.get("changes") or []
    if not changes:
        return reply
    lines = "\n".join(f"· {c}" for c in changes)
    return f"{reply}\n\n─────\n改好了：\n{lines}"

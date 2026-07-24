"""All Claude calls: persona messages, reply parsing, intent routing, Q&A."""
from __future__ import annotations

import json

from anthropic import AsyncAnthropic

from .categories import CATEGORIES
from .config import settings
from .persona import PERSONA_SYSTEM

_client = AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)
MODEL = settings.ANTHROPIC_MODEL


async def _say(user: str, system: str = PERSONA_SYSTEM, max_tokens: int = 400) -> str:
    resp = await _client.messages.create(
        model=MODEL, max_tokens=max_tokens, system=system,
        messages=[{"role": "user", "content": user}],
    )
    return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()


def _fmt(txns) -> str:
    """Number the charges 1..n for the user to reply against."""
    lines = []
    for i, t in enumerate(txns, 1):
        amt = abs(t.amount)
        lines.append(f"{i}. ${amt:.2f} — {t.merchant_desc or '(no merchant)'}")
    return "\n".join(lines)


async def enrichment_prompt(txns) -> str:
    body = _fmt(txns)
    n = len(txns)
    if n == 1:
        instr = f"剛剛出現一筆新的刷卡：\n{body}\n用你的口氣問默默這筆買了什麼。一句就好。"
    else:
        instr = (
            f"剛剛一次出現 {n} 筆新的刷卡：\n{body}\n"
            "用你的口氣一則訊息問默默每一筆各買了什麼。把編號列出來讓他好一筆一筆回。"
        )
    return await _say(instr)


async def enrichment_confirm(txns) -> str:
    summary = "\n".join(
        f"{i}. ${abs(t.amount):.2f} {t.merchant_desc} → {t.category or '未分類'}：{t.note or ''}"
        for i, t in enumerate(txns, 1)
    )
    instr = f"默默剛剛交代完這幾筆，你已經記好了：\n{summary}\n用你的口氣簡短回一句，該唸的唸一下，記好帳就好。"
    return await _say(instr)


async def parse_reply(txns, reply: str) -> dict[int, dict]:
    """Map the user's free-form reply back to each numbered charge. Returns {index: {note, category}}."""
    body = _fmt(txns)
    cats = ", ".join(CATEGORIES)
    system = (
        "You extract structured data. Return ONLY valid JSON, no prose, no code fences.\n"
        "Given numbered charges and the user's reply, output an object keyed by the charge number "
        '(as a string), each value {"note": "<what was bought, short>", "category": "<one of the allowed categories>"}.\n'
        "If the reply says to ignore/skip a charge, use category \"Transfers/Ignore\".\n"
        "If a charge is not addressed in the reply, omit it.\n"
        f"Allowed categories: {cats}"
    )
    user = f"Charges:\n{body}\n\nUser reply:\n{reply}\n\nJSON:"
    raw = await _say(user, system=system, max_tokens=600)
    raw = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        data = json.loads(raw)
    except Exception:
        return {}
    out: dict[int, dict] = {}
    for k, v in data.items():
        try:
            out[int(k)] = {"note": v.get("note"), "category": v.get("category")}
        except Exception:
            continue
    return out


async def classify_intent(text: str, has_pending: bool) -> str:
    """Route an incoming LINE message to: answer / log / question."""
    answer_line = (
        "'answer' if they are explaining charges you already asked about, or\n"
        if has_pending else ""
    )
    system = (
        "Classify the user's LINE message about their finances. Reply with ONE word only:\n"
        f"{answer_line}"
        "'log' if they are telling you about a NEW expense they just made "
        "(e.g. 'spent $12 at blue bottle', '剛剛在全聯花了 500'), or\n"
        "'question' if they are asking something or anything else."
    )
    out = (await _say(text, system=system, max_tokens=10)).lower()
    if has_pending and "answer" in out:
        return "answer"
    if "log" in out:
        return "log"
    return "question"


async def parse_manual_log(text: str) -> dict:
    """Extract {amount, merchant} from a casual expense note. Returns amount=None if unclear."""
    system = (
        "Extract an expense from the user's message. Return ONLY JSON: "
        '{"amount": <number or null>, "merchant": "<store/what, short>"}. '
        "amount is the dollar figure spent. No prose, no code fences."
    )
    raw = (await _say(text, system=system, max_tokens=120)).strip()
    raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        data = json.loads(raw)
        amt = data.get("amount")
        return {"amount": float(amt) if amt is not None else None,
                "merchant": (data.get("merchant") or "").strip()}
    except Exception:
        return {"amount": None, "merchant": ""}


async def manual_confirm(t) -> str:
    instr = (
        f"默默剛剛主動跟你報帳：${abs(t.amount):.2f}，{t.merchant_desc or '沒說是哪家'}"
        f"（分類先歸到 {t.category or '未分類'}）。用你的口氣回一句，記好了，順便虧他一下。"
    )
    return await _say(instr, max_tokens=200)


async def answer_question(question: str, data_context: str) -> str:
    instr = (
        f"默默問你：「{question}」\n\n"
        f"這是你手上的相關資料（金額都是真的，請根據它回答，不要編造）：\n{data_context}\n\n"
        "用你的口氣回答他，數字要準。"
    )
    return await _say(instr, max_tokens=900)


async def greet() -> str:
    instr = "默默第一次傳訊息給你（他的理財阿姨）。用你的口氣打個招呼，順便虧他一下說你要開始盯他花錢了。兩三句。"
    return await _say(instr)


# ── Wave 2: reports, nudges, onboarding ──────────────────────────

async def report(kind: str, data_text: str) -> str:
    label = {"weekly": "這禮拜", "monthly": "這個月", "quarterly": "這一季"}.get(kind, "這段時間")
    instr = (
        f"這是{label}的財務數據（金額都是真的）：\n{data_text}\n\n"
        f"用你的口氣幫默默做一份{label}的理財報告：先講重點，點出花太兇的地方，該唸就唸，"
        "最後一句總結或給個建議。可以分行，但不要落落長。"
    )
    return await _say(instr, max_tokens=900)


async def overspend_nudge(status: dict, level: str) -> str:
    if level == "100":
        hint = "他已經把這兩週的預算花光了，甚至超支"
    else:
        hint = "他這兩週的預算已經花掉八成以上"
    instr = (
        f"{hint}。額度 ${status['allowance']:.0f}，已經花掉 ${status['spent']:.0f}，"
        f"這一期還有 {status['days_left']} 天。用你的口氣傳一則訊息唸他、叫他收斂一點。一兩句就好。"
    )
    return await _say(instr, max_tokens=200)


async def onboarding_intro() -> str:
    instr = (
        "默默剛把你這個理財阿姨設定好。你要先問他兩件事才有辦法幫他抓預算："
        "一是他每個月固定要付多少（房租、水電、訂閱那些加起來大概），"
        "二是他想每兩週或每個月存多少錢。用你的口氣一次問清楚，親切但兇一點。"
    )
    return await _say(instr)


async def onboarding_followup(prefs_now: dict) -> str:
    missing = []
    if not prefs_now.get("fixed_monthly"):
        missing.append("每月固定支出大概多少")
    if not prefs_now.get("savings_amount"):
        missing.append("想存多少錢")
    instr = f"默默還沒講清楚這些：{'、'.join(missing)}。用你的口氣再追問一次，別讓他跳過。"
    return await _say(instr, max_tokens=200)


async def onboarding_done(prefs_now: dict) -> str:
    instr = (
        f"設定好了：每月固定支出約 ${prefs_now['fixed_monthly']:.0f}，"
        f"存錢目標 ${prefs_now['savings_amount']:.0f}（{prefs_now['savings_cadence']}）。"
        "用你的口氣跟默默確認一下，順便撂話說以後會幫他盯緊緊。"
    )
    return await _say(instr, max_tokens=200)


async def parse_onboarding(text: str) -> dict:
    system = (
        "Extract financial setup from the user's message. Return ONLY JSON, no prose, no code fences:\n"
        '{"fixed_monthly": <number or null>, "savings_amount": <number or null>, '
        '"savings_cadence": "biweekly" | "monthly" | "percent" | null}\n'
        "fixed_monthly = total fixed monthly bills (rent + utilities + subscriptions). "
        "savings_amount = how much they want to save. If they gave a percentage, put the number "
        "and set savings_cadence to 'percent'. If they said per-two-weeks use 'biweekly', per-month use 'monthly'."
    )
    raw = (await _say(text, system=system, max_tokens=150)).strip()
    raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        d = json.loads(raw)
    except Exception:
        return {"fixed_monthly": None, "savings_amount": None, "savings_cadence": None}

    def num(x):
        try:
            return float(x) if x is not None else None
        except (TypeError, ValueError):
            return None

    return {
        "fixed_monthly": num(d.get("fixed_monthly")),
        "savings_amount": num(d.get("savings_amount")),
        "savings_cadence": d.get("savings_cadence"),
    }

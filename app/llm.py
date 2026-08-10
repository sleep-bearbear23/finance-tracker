"""All Claude calls: persona messages, reply parsing, intent routing, Q&A."""
from __future__ import annotations

import base64
import json

from anthropic import AsyncAnthropic

from .categories import CATEGORIES
from .config import now, settings
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
        arrow = "收到" if t.amount > 0 else "花了"
        lines.append(f"{i}. {arrow} ${amt:.2f} — {t.merchant_desc or '(不明)'}")
    return "\n".join(lines)


async def enrichment_prompt(txns) -> str:
    body = _fmt(txns)
    n = len(txns)
    has_income = any(t.amount > 0 for t in txns)
    has_spend = any(t.amount < 0 for t in txns)
    ask = []
    if has_spend:
        ask.append("花錢的那幾筆各是買了什麼")
    if has_income:
        ask.append("收到錢的那幾筆是他自己的收入、還是別人還他錢或轉帳")
    instr = (
        f"剛剛出現 {n} 筆新的帳，要默默交代一下：\n{body}\n\n"
        f"用你的口氣一則訊息問他：{'；'.join(ask)}。把編號列出來讓他好一筆一筆回。簡短就好。"
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
        "Each numbered item is marked 花了 (money out) or 收到 (money in). Given the user's reply, "
        'output an object keyed by the number (as a string), each value '
        '{"note": "<short>", "category": "<one allowed category>", "is_income": <true|false|null>}.\n'
        "For 花了 items: fill note + category; is_income = null.\n"
        "For 收到 items: if the user says it is their OWN income/earnings/pay/client payment, "
        'is_income = true and category "Income"; if it is someone paying them back, a refund, or a transfer, '
        'is_income = false and category "Transfers/Ignore".\n'
        "If an item is not addressed in the reply, omit it.\n"
        f"Allowed categories: {cats}"
    )
    user = f"Items:\n{body}\n\nUser reply:\n{reply}\n\nJSON:"
    raw = await _say(user, system=system, max_tokens=600)
    raw = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        data = json.loads(raw)
    except Exception:
        return {}
    out: dict[int, dict] = {}
    for k, v in data.items():
        try:
            out[int(k)] = {
                "note": v.get("note"),
                "category": v.get("category"),
                "is_income": v.get("is_income"),
            }
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


async def answer_question(question: str, data_context: str, convo: str = "") -> str:
    convo_block = f"你們最近的對話（最舊到最新，你就是阿姨）：\n{convo}\n\n" if convo else ""
    instr = (
        f"{convo_block}"
        f"默默現在說：「{question}」\n\n"
        f"這是系統給你的資料（金額都是真的，要用這個，不要自己算）：\n{data_context}\n\n"
        "回他一句就好，像在傳 LINE。他問什麼你就答什麼，不要把整包預算重講一遍、也不要在對話裡自己一直加減算餘額。"
        "如果他是在回你剛剛的話，接得上就好。"
    )
    return await _say(instr, max_tokens=500)


async def parse_balance_update(text: str, account_names: list[str]) -> dict:
    """If the message states a NEW CURRENT BALANCE for one of Momo's known accounts, extract it.
    A purchase/expense is NOT a balance update. Returns amount=None when it isn't one."""
    names = "、".join(account_names)
    system = (
        "You decide if the user is telling their bookkeeper the CURRENT BALANCE of one of their "
        f"existing accounts. Their accounts: {names}.\n"
        "Return ONLY JSON, no prose, no code fences: "
        '{"name": "<one of the accounts, or null>", "amount": <number or null>, '
        '"type": "cash" | "credit" | null}.\n'
        "Set name+amount ONLY when they state what an account now holds or now owes "
        "(e.g. 'apple card 現在欠 600', 'chase 支票剩 4200', 'my apple cash is 1500 now'). "
        "type='credit' if it's what they OWE on a card, 'cash' if it's money they HAVE, else null.\n"
        "If it's a purchase, an expense, a question, or doesn't clearly name one of the accounts, "
        'return {"name": null, "amount": null, "type": null}.'
    )
    raw = (await _say(text, system=system, max_tokens=80)).strip()
    raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        d = json.loads(raw)
        amt = d.get("amount")
        return {
            "name": d.get("name"),
            "amount": float(amt) if amt is not None else None,
            "type": d.get("type"),
        }
    except Exception:
        return {"name": None, "amount": None, "type": None}


async def balance_ack(name: str, amount: float, typ, added: bool) -> str:
    what = "欠款" if typ == "credit" else "餘額"
    verb = "新記了一個帳戶" if added else "更新了"
    instr = (
        f"默默剛跟你說他的「{name}」{what}現在是 ${amount:.0f}，你{verb}。"
        "用你的口氣簡短回一句確認記好了，這種阿姨看不到的帳（像 Apple）本來就要靠他報你才知道，"
        "可以順口叮嚀他記得有變動就跟你講。一兩句就好。"
    )
    return await _say(instr, max_tokens=160)


async def profile_ack(s: dict) -> str:
    cad = "每月" if s.get("savings_cadence") == "monthly" else "每兩週"
    gig_line = f"，接下來有 {s['n_gigs']} 筆預期進帳約 ${s['gig_sum']:.0f}" if s.get("n_gigs") else ""
    acct_line = f"；{s['n_accts']} 個帳戶合計可動用現金約 ${s['cash_on_hand']:.0f}" if s.get("n_accts") else ""
    debt_line = f"（卡債／欠款約 ${s['total_debt']:.0f}）" if s.get("total_debt") else ""
    instr = (
        "默默剛把他的財務底細一次填給你了，數字如下（都是真的，你要收下當作以後抓預算的依據）：\n"
        f"今年到目前實收約 ${s['ytd_income']:.0f}；淡月底收入約 ${s['monthly_baseline']:.0f}／月；"
        f"每月固定開銷約 ${s['fixed_total']:.0f}；存錢目標 ${s['savings_amount']:.0f}（{cad}）"
        f"{acct_line}{debt_line}{gig_line}。\n"
        "用你的口氣回他一則就好：跟他確認你收到了、以後會照這些數字幫他盯，順口唸一句關心一下。"
        "不要把每個數字整包再唸一遍，簡短。"
    )
    return await _say(instr, max_tokens=240)


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


async def parse_screenshot(image_bytes: bytes, media_type: str) -> list[dict]:
    """Read a bank/credit-card screenshot and pull out the transactions."""
    b64 = base64.b64encode(image_bytes).decode()
    today = now().date().isoformat()
    system = (
        "You read a bank or credit-card transaction screenshot and extract every transaction. "
        "Return ONLY a JSON array, no prose, no code fences. Each element: "
        '{"date": "YYYY-MM-DD", "merchant": "<merchant/description>", "amount": <positive number>, '
        '"direction": "out" | "in"}. '
        "direction is 'out' for a purchase/charge/debit, 'in' for a payment/deposit/refund/credit. "
        f"Today is {today}; if a row has no year, assume the current year. "
        "Ignore balances, headers, totals, and pending-only rows without an amount."
    )
    if media_type not in ("image/jpeg", "image/png", "image/gif", "image/webp"):
        media_type = "image/jpeg"
    resp = await _client.messages.create(
        model=MODEL, max_tokens=2000, system=system,
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b64}},
            {"type": "text", "text": "Extract the transactions as JSON."},
        ]}],
    )
    raw = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
    raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        data = json.loads(raw)
        return data if isinstance(data, list) else []
    except Exception:
        return []


async def daily_reminder() -> str:
    instr = "現在是晚上，該提醒默默把今天的帳單截圖傳給你看了。用你的口氣催他一句，兇一點但別太長。"
    return await _say(instr, max_tokens=150)


async def screenshot_ack(n_recorded: int, n_dupes: int) -> str:
    extra = f"，另外有 {n_dupes} 筆是重複的我幫你跳過了" if n_dupes else ""
    instr = (
        f"默默剛傳了帳單截圖，你看完記了 {n_recorded} 筆新的{extra}。"
        "如果沒有要追問的，用你的口氣簡短回一句說記好了。"
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

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


async def parse_reply(txns, reply: str) -> dict[int, dict]:
    """Map the user's free-form reply back to each numbered charge. Returns {index: {note, category}}."""
    body = _fmt(txns)
    cats = ", ".join(f"{cid} ({zh})" for cid, (zh, _t, _n) in CATEGORIES.items())
    system = (
        "You extract structured data. Return ONLY valid JSON, no prose, no code fences.\n"
        "Each numbered item is marked 花了 (money out) or 收到 (money in). Given the user's reply, "
        'output an object keyed by the number (as a string), each value '
        '{"note": "<short>", "category": "<one allowed category id>", '
        '"inflow": "<pay|reimburse_work|reimburse_family|personal|refund|null>"}.\n'
        "For 花了 items: fill note + category; inflow = null.\n"
        "For 收到 items, pick the inflow kind from what the user says:\n"
        '  pay              — their OWN earnings: a client, a production, a day rate\n'
        '  reimburse_work   — a production paying back something they bought for the job\n'
        '  reimburse_family — parents covering a cost (car repair, a flight)\n'
        '  personal         — a friend splitting a bill or paying them back\n'
        '  refund           — a merchant refunding a purchase / a return\n'
        "For 收到 items, also set category to the ORIGINAL spending category the money "
        'reverses when it is a refund or reimbursement (e.g. a returned Amazon order is '
        '"shopping"); use "transfer" only when money simply moved between the user\'s own accounts.\n'
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
            inflow = v.get("inflow") or None
            out[int(k)] = {
                "note": v.get("note"),
                "category": v.get("category"),
                "inflow": inflow,
                # legacy consumers still read is_income; only real pay is income now
                "is_income": (True if inflow == "pay"
                              else False if inflow else v.get("is_income")),
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


async def answer_question(question: str, data_context: str, convo: str = "") -> str:
    convo_block = f"你們最近的對話（最舊到最新，你就是阿姨）：\n{convo}\n\n" if convo else ""
    instr = (
        f"{convo_block}"
        f"默默現在說：「{question}」\n\n"
        f"這是系統給你的資料（金額都是真的，要用這個，不要自己算）：\n{data_context}\n\n"
        "先把他問的那個數字直接答出來（像在傳 LINE，一兩句）。他問待收款／還沒收到的薪水／入帳後會有多少，"
        "就照資料裡那份待收款清單跟合計講給他，不要說你不清楚、也不要拿預算的收入基準去搪塞。"
        "不要把整包預算重講一遍、不要自己一直加減，也不要每次都碎念叫他別規劃——答完正事再順一句就好。"
        "如果他是在回你剛剛的話，接得上就好。"
    )
    return await _say(instr, max_tokens=500)


async def deploy_note(commit_message: str) -> str:
    instr = (
        "你（默默的理財阿姨）剛更新上線。工程師寫的更新內容是英文技術描述："
        f"「{commit_message}」。用你的口氣、台灣繁體中文，一句話跟默默說你更新好、又上工了，"
        "順便用他聽得懂的白話超短講一下這次大概弄了什麼，不要照抄英文、不要念技術名詞。就一句。"
    )
    try:
        return await _say(instr, max_tokens=120)
    except Exception:
        return "默默，阿姨更新好、又上工了。"


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

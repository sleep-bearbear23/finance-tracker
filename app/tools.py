"""What 陳會計 is allowed to actually do, as tools she calls rather than words she says.

The old routing guessed intent from about twenty hard-coded words — 入帳, 接了, 改成 —
and anything outside that list fell through to free-text Q&A, where she has a voice and
no hands. So 「我九月的 Avia 檔期確定了 9/6-9/15，大概 $2800」 was answered with 「記起來了，
幫你加一筆待收款」 and nothing was written. Worse, on the way down it was offered to a
balance parser (a sentence about income, asked "which account balance is this?") and to
an intent classifier that could have logged $2,800 as an *expense*.

Every write lives here now, with a real schema. The loop in :mod:`app.agent` runs the
tools first and lets her speak afterwards, so 「記起來了」 is only sayable when something
returned ok. Each handler returns ``{"ok": bool, "summary": str}`` where the summary is
the concrete before→after line Momo reads — that reporting is the condition she attached
to giving 陳會計 full authority.
"""
from __future__ import annotations

import json
from datetime import datetime

from . import categories, changelog, fixed, prefs, record
from .config import TZ, now
from .db import get_kv, set_kv
from .models import MerchantMemory, Transaction

CADENCES = ("monthly", "quarterly", "semiannual", "annual")
_STAGE_ZH = {"booked": "已接（還沒拍）", "wrapped": "已殺青", "invoiced": "已開發票"}
_CAD_ZH = {"monthly": "每月", "quarterly": "每季", "semiannual": "每半年", "annual": "每年"}


def categories_is_work(cat: str | None) -> bool:
    from . import taxonomy as _T
    return _T.is_work(cat)


def _money(v) -> str:
    return f"${float(v or 0):,.2f}".replace(".00", "")


# ── schemas ──────────────────────────────────────────────────────────
SCHEMAS: list[dict] = [
    {
        "name": "add_expected_payment",
        "description": (
            "Record a NEW job Momo has been booked for, or any money she is now waiting "
            "on. Use this the moment she says a shoot is confirmed, she took a gig, a "
            "production owes her, or an invoice went out — she does not have to use the "
            "word 'invoice'. 「九月的 Avia 檔期確定了 9/6-9/15，大概 $2800」 is this tool."),
        "input_schema": {
            "type": "object",
            "properties": {
                "amount": {"type": "number", "description": "Expected payment in USD."},
                "note": {"type": "string",
                         "description": "What the job is, in her words — production, dates, role."},
                "when": {"type": "string",
                         "description": "YYYY-MM of the WORK — the month the shoot happened "
                                        "or wrapped, not the month a cheque might arrive. "
                                        "Payment lands about two weeks after that month "
                                        "ends, and the whole app derives the date from this "
                                        "one field. NEVER move it forward to make a late "
                                        "job look on time: lateness is measured from it, "
                                        "and an old job counts for less on purpose."},
                "days": {"type": "integer",
                         "description": "Shoot days. She almost always says it — 「拍八天」 — and "
                                        "it is what her day rate is computed from, so capture it "
                                        "whenever she does."},
                "stage": {"type": "string", "enum": ["booked", "wrapped", "invoiced"],
                          "description": "booked = the shoot has not happened yet (default). "
                                         "wrapped = the work is done. invoiced = the invoice "
                                         "is in. A booked job is riskier than a wrapped one and "
                                         "counts for less, so do not call something wrapped "
                                         "until she says it wrapped."},
                "wrapped_on": {"type": "string",
                               "description": "YYYY-MM-DD the shoot finished, when known. The "
                                              "payment clock runs from this, not from the month."},
                "expect_on": {"type": "string",
                              "description": "YYYY-MM-DD, but ONLY when a production actually "
                                             "told her a date — 「他們說這個月底付」. It overrides "
                                             "the estimate outright. Never invent one; without "
                                             "it the date is derived from the wrap plus the "
                                             "usual lag, which is the honest default."},
                "force": {"type": "boolean",
                          "description": "Only after Momo confirms it is genuinely a second, "
                                         "separate job — not the one already on the list."},
            },
            "required": ["amount", "note"],
        },
    },
    {
        "name": "update_expected_payment",
        "description": (
            "Change a payment she is already waiting on: the fee moved, the month "
            "slipped, the description was wrong. Match on `which` by name or amount."),
        "input_schema": {
            "type": "object",
            "properties": {
                "which": {"type": "string", "description": "Name or amount of the existing payment."},
                "amount": {"type": "number"},
                "when": {"type": "string",
                         "description": "YYYY-MM of the WORK, not of the hoped-for payment. "
                                        "Correct it BACKWARDS when Momo says a job wrapped "
                                        "earlier than recorded — that is what makes an "
                                        "overdue invoice count for less instead of "
                                        "flattering the plan."},
                "note": {"type": "string"},
                "days": {"type": "integer", "description": "Shoot days, if she says them now."},
                "stage": {"type": "string", "enum": ["booked", "wrapped", "invoiced"],
                          "description": "Move it along the pipeline. 「這個殺青了」 → wrapped, "
                                         "「發票開出去了」 → invoiced. Each step retires real risk "
                                         "and the money counts for more."},
                "wrapped_on": {"type": "string",
                               "description": "YYYY-MM-DD it wrapped. Setting this also moves the "
                                              "stage to wrapped and restarts the payment clock "
                                              "from the real date."},
                "expect_on": {"type": "string",
                              "description": "YYYY-MM-DD, but ONLY when a production actually "
                                             "told her a date — 「他們說這個月底付」. It overrides "
                                             "the estimate outright. Never invent one; without "
                                             "it the date is derived from the wrap plus the "
                                             "usual lag, which is the honest default."},
                "confidence": {"type": "number",
                               "description": "0–1. How much of this one to actually count on. "
                                              "Set it ONLY when Momo says something about whether "
                                              "this production pays — 「這家一向準時」 → 1, "
                                              "「這筆我覺得要不回來了」 → 0.2. Her view beats the "
                                              "stage and the lateness both, because she knows "
                                              "who pays and the model does not."},
            },
            "required": ["which"],
        },
    },
    {
        "name": "start_new_season",
        "description": (
            "Reset the three-month earning target and start the scoreboard over from today. "
            "Use when Momo says a chapter is done and she wants to start counting again — "
            "「重新開始算這一季」. It freezes today's targets; work already booked shows as the "
            "starting position, not as progress she made."),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "mark_payment_received",
        "description": "The money finally landed. Takes it off the waiting list.",
        "input_schema": {
            "type": "object",
            "properties": {"which": {"type": "string"}},
            "required": ["which"],
        },
    },
    {
        "name": "remove_expected_payment",
        "description": (
            "The job fell through, or the payment was never real. Deletes it. Use "
            "mark_payment_received instead when the money actually arrived."),
        "input_schema": {
            "type": "object",
            "properties": {"which": {"type": "string"}},
            "required": ["which"],
        },
    },
    {
        "name": "set_account_balance",
        "description": (
            "Set what one account or card is at right now, for the accounts that do not "
            "sync (Apple Card, Apple Savings, Venmo). Only call this when Momo is stating "
            "a BALANCE. A fee she will be paid is not a balance."),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "amount": {"type": "number", "description": "Positive. For a card this is what is OWED."},
                "kind": {"type": "string", "enum": ["cash", "credit"]},
            },
            "required": ["name", "amount"],
        },
    },
    {
        "name": "add_fixed_cost",
        "description": (
            "Add a recurring bill or subscription to the fixed costs the budget subtracts "
            "every period. 「幫我加 Claude 訂閱一個月 $100」 is this tool. Fixed costs had no "
            "write path at all before, which is why two Anthropic subscriptions were "
            "reported as added and were not."),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "amount": {"type": "number", "description": "The amount of ONE payment, not per month."},
                "cadence": {"type": "string", "enum": list(CADENCES)},
                "category": {"type": "string",
                             "description": "Category id, e.g. subs, insurance, rent, phone, gas, car."},
                "next_due": {"type": "string", "description": "YYYY-MM-DD of the next charge, if known."},
                "where": {"type": "string", "description": "Which card or account it comes out of."},
                "note": {"type": "string"},
                "manual": {"type": "boolean",
                           "description": "True when Momo has to go and DO it every time — a "
                                          "Zelle to her mother, a transfer. Those belong on "
                                          "the calendar every month; an auto-debit like Adobe "
                                          "does not, and would just be noise."},
                "force": {"type": "boolean",
                          "description": "Only after Momo confirms she really pays both. Adding "
                                         "a second line for a bill she already has silently "
                                         "doubles it in every budget. You are shown the full "
                                         "list of her fixed costs — READ IT before adding, and "
                                         "if something already covers this, update that row "
                                         "instead of adding a second one under a new name."},
            },
            "required": ["name", "amount", "cadence"],
        },
    },
    {
        "name": "update_fixed_cost",
        "description": "A subscription's price changed, or its billing date moved.",
        "input_schema": {
            "type": "object",
            "properties": {
                "which": {"type": "string"},
                "amount": {"type": "number"},
                "cadence": {"type": "string", "enum": list(CADENCES)},
                "next_due": {"type": "string"},
                "name": {"type": "string", "description": "New name, if renaming."},
            },
            "required": ["which"],
        },
    },
    {
        "name": "remove_fixed_cost",
        "description": "She cancelled a subscription or no longer pays a bill.",
        "input_schema": {
            "type": "object",
            "properties": {"which": {"type": "string"}},
            "required": ["which"],
        },
    },
    {
        "name": "set_savings_plan",
        "description": "Change how much she is trying to put away, and how often.",
        "input_schema": {
            "type": "object",
            "properties": {
                "amount": {"type": "number"},
                "cadence": {"type": "string", "enum": ["biweekly", "monthly"],
                            "description": "biweekly means per half-month period, not every two weeks."},
            },
            "required": ["amount", "cadence"],
        },
    },
    {
        "name": "set_day_rate",
        "description": (
            "Pin what Momo charges per shoot day, which is what the earning goal divides "
            "by to turn a dollar gap into a number of days. Call it when she states her "
            "rate. Pass 0 to go back to computing it from her recorded jobs. Her own "
            "figure beats the average on purpose — the average still carries older, "
            "cheaper gigs and so asks for days she would not actually need."),
        "input_schema": {
            "type": "object",
            "properties": {"amount": {"type": "number", "description": "Dollars per shoot day. 0 = automatic."}},
            "required": ["amount"],
        },
    },
    {
        "name": "raise_daily",
        "description": (
            "Momo wants to spend more than today's line allows. The raise is funded ONLY "
            "from 本期口袋 (what she saved on earlier days) — if the pool cannot cover it "
            "the tool refuses and tells her the most it can do. Call this when she asks to "
            "spend a bit more today or for the rest of the period. Do NOT call it just "
            "because she spent over; overspending already comes out of the pool by itself."),
        "input_schema": {
            "type": "object",
            "properties": {
                "amount": {"type": "number", "description": "Extra dollars PER DAY."},
                "days": {"type": "integer",
                         "description": "How many days it applies to. Omit for the rest of the period; 1 for just today."},
            },
            "required": ["amount"],
        },
    },
    {
        "name": "close_session",
        "description": (
            "End-of-period settlement. Run it when the half-month is ending or has just "
            "ended, or when Momo asks how the period went. It reports how many days she "
            "held the line and settles 本期口袋: 'quarter' puts it toward the season goal "
            "(the usual choice), 'carry' leaves it for the next period, 'none' does "
            "nothing. Ask her which before calling — this is meant to be a conversation, "
            "not a silent rollover."),
        "input_schema": {
            "type": "object",
            "properties": {
                "destination": {"type": "string", "enum": ["quarter", "carry", "none"]},
                "note": {"type": "string", "description": "Anything she wants recorded about the period."},
            },
            "required": ["destination"],
        },
    },
    {
        "name": "set_defend_rung",
        "description": (
            "Change how many months of survival money Momo holds back before anything "
            "counts as spendable — 第一階 is 1 month, 第二階 is 2, 第三階 is 3. Default 1. "
            "Raising it is how she deliberately climbs; it makes this period's allowance "
            "SMALLER, so only call it when she has actually said she wants to build the "
            "cushion up. Never call it because a payment landed or because her balance "
            "went up — the whole point is that the level only moves when she decides."),
        "input_schema": {
            "type": "object",
            "properties": {
                "months": {"type": "number",
                           "description": "1 = 第一階, 2 = 第二階, 3 = 第三階."},
            },
            "required": ["months"],
        },
    },
    {
        "name": "set_emergency_target",
        "description": (
            "Pin the emergency fund goal to a number she chose. Pass 0 to unpin it and go "
            "back to the computed target, which is re-derived from her own volatility "
            "every half-month."),
        "input_schema": {
            "type": "object",
            "properties": {"amount": {"type": "number"}},
            "required": ["amount"],
        },
    },
    {
        "name": "set_income_baseline",
        "description": (
            "What she can count on in a bad month, per month. This is the floor the budget "
            "plans against, not what she hopes for."),
        "input_schema": {
            "type": "object",
            "properties": {"amount": {"type": "number"}},
            "required": ["amount"],
        },
    },
    {
        "name": "log_expense",
        "description": (
            "She spent money that will not show up in a bank feed — cash, or a card that "
            "does not sync. Only for money that has ALREADY left."),
        "input_schema": {
            "type": "object",
            "properties": {
                "amount": {"type": "number", "description": "Positive; it is recorded as a spend."},
                "merchant": {"type": "string"},
                "category": {"type": "string"},
                "note": {"type": "string"},
                "date": {"type": "string", "description": "YYYY-MM-DD. Defaults to today."},
                "project": {"type": "string",
                            "description": "The job this was spent on, if it was for work. "
                                           "Setting it files the charge as 工作支出 whatever "
                                           "was bought, so it stays out of her daily budget."},
                "reimbursable": {"type": "boolean",
                                 "description": "True when a production will pay it back."},
            },
            "required": ["amount", "merchant"],
        },
    },
    {
        "name": "tag_project",
        "description": (
            "Move charges she ALREADY logged onto a job. Use it when she says something "
            "she told you about earlier was actually for a shoot, or will be reimbursed. "
            "They become 工作支出 and stop counting against her daily allowance. "
            "IMPORTANT: money spent for a production is 工作 whatever it bought — a taxi "
            "to set is not 交通雜支 and lunch on set is not 食. Ask which job it was."),
        "input_schema": {
            "type": "object",
            "properties": {
                "which": {"type": "string",
                          "description": "Merchant name or exact amount to find the charges by."},
                "project": {"type": "string", "description": "Which job."},
                "reimbursable": {"type": "boolean",
                                 "description": "True when the production pays it back."},
                "days": {"type": "integer", "description": "How far back to look. Default 30."},
            },
            "required": ["which", "project"],
        },
    },
    {
        "name": "answer_pending_charges",
        "description": (
            "Momo is telling you what the numbered charges you asked about actually were. "
            "Pass her message through word for word — the parser needs her own phrasing to "
            "line the numbers up. Only call this when there ARE charges waiting; the list "
            "is shown to you above when there is one."),
        "input_schema": {
            "type": "object",
            "properties": {
                "reply": {"type": "string", "description": "Momo's message, verbatim."},
            },
            "required": ["reply"],
        },
    },
    {
        "name": "log_income",
        "description": (
            "Money Momo has ALREADY BEEN PAID that no bank feed will ever show — cash in "
            "hand, or a payment into an account that does not sync. 「6/25-6/26 有一個小專案賺"
            "了 $250 現金」 is this tool, in ONE call.\n"
            "Do NOT use add_expected_payment + mark_payment_received for this. That pair "
            "adds a row to the waiting list and then deletes it, so the money is never "
            "recorded anywhere — it happened, and $250 of real income vanished.\n"
            "add_expected_payment is for money she is still WAITING for. This is for money "
            "already in her hand."),
        "input_schema": {
            "type": "object",
            "properties": {
                "amount": {"type": "number", "description": "Positive."},
                "source": {"type": "string", "description": "Who paid, or what the job was."},
                "date": {"type": "string", "description": "YYYY-MM-DD she was paid. Defaults to today."},
                "account": {"type": "string",
                            "description": "Where it went, if she said — 現金, Venmo, Apple. "
                                           "Naming it also adds the money to that balance, "
                                           "which is otherwise a second thing she has to "
                                           "remember to tell you."},
                "note": {"type": "string"},
            },
            "required": ["amount", "source"],
        },
    },
    {
        "name": "remember_merchant",
        "description": (
            "Remember what a merchant or sender is, so it is never asked about again, and "
            "so past charges from it can be re-tagged. Use when she corrects a category."),
        "input_schema": {
            "type": "object",
            "properties": {
                "merchant": {"type": "string"},
                "category": {"type": "string"},
                "kind": {"type": "string", "enum": ["spend", "income", "transfer"]},
                "note": {"type": "string"},
            },
            "required": ["merchant"],
        },
    },
]

NAMES = [s["name"] for s in SCHEMAS]


# ── handlers ─────────────────────────────────────────────────────────
def _resolve(items: list[dict], which: str, field: str = "name") -> tuple[dict | None, list[dict]]:
    """Find the one row `which` means. Returns (hit, ambiguous_candidates).

    Exact name wins over a partial one — matching loosely and taking the first hit meant
    「Claude 訂閱」 resolved to 「Claude 訂閱（含加值）」, so deleting the one she meant deleted
    the one she did not. When a partial match is genuinely ambiguous, nobody guesses: the
    candidates come back and she gets asked."""
    key = prefs._norm(which)
    if key:
        exact = [it for it in items if prefs._norm(str(it.get(field) or "")) == key]
        if len(exact) == 1:
            return exact[0], []
        if len(exact) > 1:
            return None, exact
        part = [it for it in items
                if (n := prefs._norm(str(it.get(field) or ""))) and (key in n or n in key)]
        if len(part) == 1:
            return part[0], []
        if len(part) > 1:
            return None, part
    try:
        amt = float(str(which).replace("$", "").replace(",", ""))
    except (TypeError, ValueError):
        return None, []
    by_amt = [it for it in items if abs(float(it.get("amount") or 0) - amt) < 0.5]
    if len(by_amt) == 1:
        return by_amt[0], []
    return None, by_amt if len(by_amt) > 1 else []


def _find(items: list[dict], which: str, field: str = "name") -> dict | None:
    return _resolve(items, which, field)[0]


def _miss(which: str, items: list[dict], field: str, ambiguous: list[dict], what: str) -> dict:
    """The honest failure. Either 'there is no such thing' or 'which of these two'."""
    if ambiguous:
        return {"ok": False, "summary": "", "candidates": [a.get(field) for a in ambiguous],
                "error": (f"「{which}」對到 {len(ambiguous)} 筆："
                          + "、".join(f"{a.get(field)} {_money(a.get('amount'))}" for a in ambiguous)
                          + "。你是說哪一筆？")}
    return {"ok": False, "summary": "", "have": [i.get(field) for i in items],
            "error": f"{what}裡找不到「{which}」"}


async def add_expected_payment(s, rec, amount, note, when=None, days=None,
                               stage=None, wrapped_on=None, expect_on=None, force=False):
    # A duplicate here is not a cosmetic problem: 待收款 feeds the projection and the
    # 需要賺 index, so the same job entered twice tells her she can afford things twice.
    if not force:
        dup = _find(prefs._load_list(await get_kv(s, "cfg_upcoming")), note, "note")
        if dup and abs(float(dup.get("amount") or 0) - float(amount)) < 1:
            return {"ok": False, "summary": "", "duplicate": dup,
                    "error": (f"已經有一筆很像的：「{dup.get('note')}」{_money(dup.get('amount'))}"
                              f"（{dup.get('when') or '未定'}）。是要改那一筆，還是真的是另一個案子？"
                              "確定是新的再用 force=true。")}
    item = await prefs.add_invoice(s, amount, when, note, stage=stage,
                                   wrapped_on=wrapped_on, days=days, expect_on=expect_on)
    land = prefs.landing(item)
    st = prefs.stage_of(item)
    conf = prefs.confidence(item)
    rec.says(f"加了一筆待收款：{note} {_money(amount)}"
             + (f"（{days} 天，一天 {_money(float(amount) / days)}）" if days else "")
             + f"　{_STAGE_ZH[st]}，先當 {conf:.0%} 算"
             + (f"，預估 {land} 入帳" if land else "，還沒說什麼時候"))
    return {"ok": True, "summary": rec.summary, "item": item}


async def update_expected_payment(s, rec, which, amount=None, when=None, note=None,
                                  days=None, stage=None, wrapped_on=None, expect_on=None,
                                  confidence=None):
    items = prefs._load_list(await get_kv(s, "cfg_upcoming"))
    old, amb = _resolve(items, which, "note")
    if old is None:
        return _miss(which, items, "note", amb, "待收款")
    before = dict(old)
    hit = await prefs.update_invoice(s, which, amount, when, note)
    extra = {k: v for k, v in (("days", days), ("stage", stage), ("expect_on", expect_on),
                               ("wrapped_on", wrapped_on), ("confidence", confidence))
             if v is not None}
    if extra and hit is not None:
        items = prefs._load_list(await get_kv(s, "cfg_upcoming"))
        row = _find(items, hit.get("note") or which, "note")
        if row is not None:
            if "confidence" in extra:
                extra["confidence"] = min(1.0, max(0.0, float(extra["confidence"])))
            if "wrapped_on" in extra:
                extra["wrapped_on"] = str(extra["wrapped_on"])[:10]
                extra.setdefault("stage", "wrapped")
            row.update(extra)
            await set_kv(s, "cfg_upcoming", json.dumps(items, ensure_ascii=False))
            hit = dict(row)
    bits = []
    if amount is not None and float(before.get("amount") or 0) != float(amount):
        bits.append(f"{_money(before.get('amount'))} → {_money(amount)}")
    if when and before.get("when") != when:
        bits.append(f"{before.get('when') or '未定'} → {when}")
    if note and before.get("note") != note:
        bits.append(f"備註改成「{note}」")
    if stage or wrapped_on:
        st = prefs.stage_of(hit or before)
        was = prefs.stage_of(before)
        bits.append(f"{_STAGE_ZH[was]} → {_STAGE_ZH[st]}"
                    + (f"（{wrapped_on} 殺青）" if wrapped_on else "")
                    + f"，信心 {prefs.confidence(before):.0%} → {prefs.confidence(hit or before):.0%}")
    if days is not None:
        bits.append(f"拍 {days} 天")
    if expect_on:
        bits.append(f"對方說 {str(expect_on)[:10]} 付，預估入帳日改成這天")
    if confidence is not None:
        bits.append(f"這筆只當 {float(confidence):.0%} 算")
    rec.says(f"改了待收款「{before.get('note')}」：" + ("、".join(bits) or "沒有實際變動"))
    return {"ok": True, "summary": rec.summary, "item": hit}


async def mark_payment_received(s, rec, which):
    items = prefs._load_list(await get_kv(s, "cfg_upcoming"))
    old, amb = _resolve(items, which, "note")
    if old is None:
        return _miss(which, items, "note", amb, "待收款")
    hit = await prefs.mark_invoice(s, which, "received")
    rec.says(f"「{old.get('note')}」{_money(old.get('amount'))} 入帳了，從待收款劃掉")
    return {"ok": True, "summary": rec.summary, "item": hit}


async def remove_expected_payment(s, rec, which):
    items = prefs._load_list(await get_kv(s, "cfg_upcoming"))
    old, amb = _resolve(items, which, "note")
    if old is None:
        return _miss(which, items, "note", amb, "待收款")
    kept = [i for i in items if i is not old]
    await set_kv(s, "cfg_upcoming", json.dumps(kept, ensure_ascii=False))
    rec.says(f"刪掉待收款「{old.get('note')}」{_money(old.get('amount'))}")
    return {"ok": True, "summary": rec.summary}


async def set_account_balance(s, rec, name, amount, kind=None):
    accts = prefs._load_list(await get_kv(s, "cfg_accounts"))
    old = next((a for a in accts if prefs._norm(a.get("name")) == prefs._norm(name)), None)
    res = await prefs.update_account(s, name, amount, kind)
    if old is None:
        rec.says(f"新增帳戶 {res['name']}：{_money(amount)}")
    else:
        rec.says(f"{res['name']} {_money(old.get('amount'))} → {_money(amount)}")
    return {"ok": True, "summary": rec.summary, **res}


async def add_fixed_cost(s, rec, name, amount, cadence, category=None,
                         next_due=None, where=None, note=None, manual=False, force=False):
    if cadence not in CADENCES:
        return {"ok": False, "summary": "", "error": f"cadence 只能是 {CADENCES}"}
    rows = await fixed.rows(s, include_sinking=False)
    # 「Claude 訂閱」 added on top of 「Claude 訂閱（含加值）」 quietly charges her twice, every
    # month, in every lens. Refuse and make somebody choose.
    if not force:
        dup = _find(rows, name)
        if dup is None:
            # 「給家人的錢 $1,000／月」 was added next to 「房租（Zelle 給媽媽）$1,000／月」 —
            # the same payment under a name that shares no characters, so matching on the
            # name found nothing and her rent was counted twice. Same amount + same cadence
            # is worth stopping for even when the names look unrelated.
            dup = next((r for r in rows
                        if abs(float(r.get("amount") or 0) - float(amount)) < 0.5
                        and (r.get("cadence") or "monthly") == cadence), None)
        if dup is not None:
            return {"ok": False, "summary": "", "duplicate": dup,
                    "error": (f"固定開銷裡已經有「{dup.get('name')}」"
                              f"{_CAD_ZH.get(dup.get('cadence'), '')} {_money(dup.get('amount'))}"
                              "，金額跟週期都一樣。是同一筆嗎？"
                              "如果是，用 update_fixed_cost 改那一筆（可以順便改名字）；"
                              "真的是兩筆不同的錢再用 force=true。")}
    rows.append({"name": name, "amount": float(amount), "cadence": cadence,
                 "cat": category, "where": where, "next_due": next_due, "note": note,
                 "manual": bool(manual) or None})
    await fixed.save(s, rows)
    per_month = float(amount) / fixed.CADENCE_MONTHS[cadence]
    total = await fixed.monthly_total(s)
    rec.says(f"加了固定開銷 {name}：{_CAD_ZH[cadence]} {_money(amount)}"
             + (f"（換算每月 {_money(per_month)}）" if cadence != "monthly" else "")
             + f"，固定開銷總額變成每月 {_money(total)}")
    return {"ok": True, "summary": rec.summary, "monthly_total": total}


async def update_fixed_cost(s, rec, which, amount=None, cadence=None, next_due=None, name=None):
    rows = await fixed.rows(s, include_sinking=False)
    hit, amb = _resolve(rows, which)
    if hit is None:
        return _miss(which, rows, "name", amb, "固定開銷")
    before = dict(hit)
    if amount is not None:
        hit["amount"] = float(amount)
    if cadence in CADENCES:
        hit["cadence"] = cadence
    if next_due:
        hit["next_due"] = next_due
    if name:
        hit["name"] = name
    await fixed.save(s, rows)
    total = await fixed.monthly_total(s)
    bits = []
    if amount is not None and float(before.get("amount") or 0) != float(amount):
        bits.append(f"{_money(before.get('amount'))} → {_money(amount)}")
    if cadence and before.get("cadence") != cadence:
        bits.append(f"{_CAD_ZH.get(before.get('cadence'), '')} → {_CAD_ZH[cadence]}")
    if next_due and before.get("next_due") != next_due:
        bits.append(f"下次扣款 {before.get('next_due') or '未定'} → {next_due}")
    if name and before.get("name") != name:
        bits.append(f"改名為「{name}」")
    rec.says(f"改了固定開銷「{before.get('name')}」：" + ("、".join(bits) or "沒有實際變動")
             + f"，固定開銷總額每月 {_money(total)}")
    return {"ok": True, "summary": rec.summary, "monthly_total": total}


async def remove_fixed_cost(s, rec, which):
    rows = await fixed.rows(s, include_sinking=False)
    hit, amb = _resolve(rows, which)
    if hit is None:
        return _miss(which, rows, "name", amb, "固定開銷")
    await fixed.save(s, [r for r in rows if r is not hit])
    total = await fixed.monthly_total(s)
    rec.says(f"刪掉固定開銷「{hit.get('name')}」{_money(hit.get('amount'))}"
             f"，固定開銷總額每月 {_money(total)}")
    return {"ok": True, "summary": rec.summary, "monthly_total": total}


async def set_savings_plan(s, rec, amount, cadence):
    p = await prefs.get_prefs(s)
    await prefs.set_prefs(s, savings_amount=float(amount),
                          savings_cadence="monthly" if cadence == "monthly" else "biweekly")
    zh = "每月" if cadence == "monthly" else "每半個月"
    was = "每月" if p["savings_cadence"] == "monthly" else "每半個月"
    rec.says(f"存錢目標 {was} {_money(p['savings_amount'])} → {zh} {_money(amount)}")
    return {"ok": True, "summary": rec.summary}


async def set_emergency_target(s, rec, amount):
    old = await get_kv(s, "cfg_emergency_target")
    await set_kv(s, "cfg_emergency_target", str(float(amount)))
    if float(amount) <= 0:
        rec.says(f"緊急預備金目標從 {_money(old)} 改回「自動計算」，每半個月依你的收入起伏重算")
    else:
        rec.says(f"緊急預備金目標 {_money(old) if old else '自動計算'} → {_money(amount)}（鎖定）")
    return {"ok": True, "summary": rec.summary}


async def set_defend_rung(s, rec, months):
    """Climb, or step back down. The whole point of the rung being a decision."""
    from . import allowance as AL
    old = await AL.defend_months(s)
    new = await AL.set_defend_months(s, months)
    a = await AL.compute(s)
    where = f"＝ {_money(a['defended_floor'])}"
    if new > old:
        rec.says(f"守住的水位從 {old:g} 個月升到 {new:g} 個月 {where}。"
                 f"這一期能花的會變少（{_money(a['allowance'])}），"
                 "因為你決定把更多錢留在底下那一層。")
    elif new < old:
        rec.says(f"守住的水位從 {old:g} 個月降到 {new:g} 個月 {where}，"
                 f"這一期能花的變成 {_money(a['allowance'])}。")
    else:
        rec.says(f"守住的水位本來就是 {new:g} 個月 {where}，沒有變。")
    return {"ok": True, "summary": rec.summary, "months": new,
            "floor": a["defended_floor"], "allowance": a["allowance"]}


async def raise_daily(s, rec, amount, days=None):
    """Raise today's line, paid for out of 本期口袋 and nowhere else.

    The constraint is the feature. Momo: "that raise would ONLY come out of that pool."
    A raise the pool cannot cover is not a refusal to let her live — it means the money has
    not been saved up yet, and saying so is the honest version of no."""
    from datetime import date as _date
    from datetime import timedelta as _td
    from . import allowance as AL
    from . import period as P

    a = await AL.compute(s)
    dv, key = a["daily"], a["period_key"]
    _, hi = P.key_bounds(key)
    today = now().date()
    if dv["days_left"] <= 0:
        return {"ok": False, "error": "這一期已經結束了，加不了。"}

    amount = round(abs(float(amount)), 2)
    span = int(days) if days else dv["days_left"]
    span = max(1, min(span, dv["days_left"]))
    cost = round(amount * span, 2)

    if cost > dv["pool"] + 0.005:
        afford = round(dv["pool"] / span, 2) if span else 0.0
        return {"ok": False, "capped": True, "pool": dv["pool"],
                "error": (f"口袋只有 {_money(dv['pool'])}，加 {_money(amount)}／天 × {span} 天 "
                          f"要 {_money(cost)}，不夠。最多能加 {_money(afford)}／天——"
                          "這不是不給你花，是那筆錢還沒省出來。")}

    until = min(hi, today + _td(days=span - 1))
    await AL.add_grant(s, key, amount, today, until)
    after = (await AL.compute(s))["daily"]
    rec.says(f"{today.isoformat()}–{until.isoformat()} 每天多 {_money(amount)}"
             f"（共 {_money(cost)}，從口袋出）。今天可以花 {_money(after['daily_today'])}，"
             f"口袋剩 {_money(dv['pool'] - cost)}。")
    return {"ok": True, "summary": rec.summary, "granted": amount, "days": span,
            "cost": cost, "daily_today": after["daily_today"],
            "pool_after": round(dv["pool"] - cost, 2)}


async def close_session(s, rec, destination="quarter", note=None):
    """End-of-period settlement: what the pool becomes.

    Momo wanted a closing conversation rather than a silent rollover — a summary of how the
    fortnight went, and a decision about the money she did not spend. 'quarter' sends it to
    the season goal, 'carry' leaves it for the next fortnight."""
    from . import allowance as AL
    c = await AL.closure(s)
    pool = c["pool"]
    if destination not in ("quarter", "carry", "none"):
        return {"ok": False, "error": "destination 只能是 quarter / carry / none"}

    if pool > 0 and destination == "quarter":
        old = await get_kv(s, "cfg_season_pot")
        try:
            pot = float(old or 0)
        except (TypeError, ValueError):
            pot = 0.0
        await set_kv(s, "cfg_season_pot", str(round(pot + pool, 2)))
        rec.says(f"{c['label']} 結算：{c['days_under']} 天守住、{c['days_over']} 天超過，"
                 f"口袋 {_money(pool)} 放進這一季的目標，季目標存款累計 {_money(pot + pool)}。")
    elif pool > 0:
        rec.says(f"{c['label']} 結算：{c['days_under']} 天守住、{c['days_over']} 天超過，"
                 f"口袋 {_money(pool)} " + ("留到下一期。" if destination == "carry" else "先不動。"))
    else:
        rec.says(f"{c['label']} 結算：{c['days_under']} 天守住、{c['days_over']} 天超過，口袋沒有剩。")
    if note:
        rec.says(note)
    return {"ok": True, "summary": rec.summary, "closure": c, "destination": destination}


async def set_day_rate(s, rec, amount):
    """Pin what she charges per shoot day. 0 hands it back to the observed figure."""
    from . import analytics as AN
    old = await prefs.pinned_day_rate(s)
    new = await prefs.set_pinned_day_rate(s, amount)
    dr = (await AN.to_book(s))["day_rate"]
    if new <= 0:
        rec.says(f"日薪改回自動算：帳上 {dr['n']} 個案子、{dr['days']} 天，"
                 f"最近幾個 {_money(dr['recent'])}／天，全部平均 {_money(dr['observed'])}／天。")
    else:
        rec.says(f"日薪定 {_money(new)}／天"
                 + (f"（本來 {_money(old)}）" if old else "")
                 + f"。帳上實際平均是 {_money(dr['observed'])}／天，最近幾個 {_money(dr['recent'])}／天——"
                 "定價用你說的，記錄放旁邊當參考。")
    return {"ok": True, "summary": rec.summary, "day_rate": dr}


async def set_income_baseline(s, rec, amount):
    old = await get_kv(s, "cfg_monthly_baseline")
    await set_kv(s, "cfg_monthly_baseline", str(float(amount)))
    rec.says(f"淡月底收入 {_money(old) if old else '未設定'} → 每月 {_money(amount)}")
    return {"ok": True, "summary": rec.summary}


async def tag_project(s, rec, which, project, reimbursable=None, days=30):
    """Move charges she has already logged onto a job, and out of her daily allowance.

    This is the repair path for the thing that went wrong: she told 陳會計 a taxi and a
    meal were for a shoot and would be paid back, and they were filed as 交通雜支 and 食 —
    the right answer to "what was bought" and the wrong answer to "whose money is it".
    They then came out of her daily line.
    """
    from sqlalchemy import select
    from . import taxonomy as T
    from . import period as P
    from datetime import timedelta as _td

    from . import projects as PJ
    key = (which or "").strip().lower()
    if not key:
        return {"ok": False, "error": "要說是哪一筆（商家名字或金額）"}
    hit = await PJ.resolve(s, project)
    if hit["id"] is None:
        names = "、".join(f"{o['name']}" for o in hit["options"][:4])
        return {"ok": False, "ambiguous": hit["options"],
                "error": f"「{project}」對得上好幾個案子：{names}。是哪一個？"}
    pid = hit["id"]
    cutoff = now().date() - _td(days=int(days or 30))

    rows = (await s.execute(select(Transaction))).scalars().all()
    hits = []
    for t in rows:
        if t.amount >= 0:
            continue
        d = (t.posted_at or t.created_at)
        if d and d.date() < cutoff:
            continue
        hay = f"{t.merchant_desc or ''} {t.note or ''}".lower()
        amt_match = False
        try:
            amt_match = abs(abs(t.amount) - float(key.replace("$", "").replace(",", ""))) < 0.02
        except ValueError:
            pass
        if key in hay or amt_match:
            hits.append(t)
    if not hits:
        return {"ok": False, "error": f"最近 {days} 天找不到「{which}」這筆。"}

    moved, total = [], 0.0
    for t in hits:
        before = changelog.snapshot_row(t, ["category", "project", "reimbursable", "status"])
        was = T.label(t.category)
        t.category = "work"
        t.project = pid
        if reimbursable is not None:
            t.reimbursable = bool(reimbursable)
        t.status = "enriched"
        total += abs(t.amount)
        moved.append(f"{(t.merchant_desc or '')[:18]} {_money(abs(t.amount))}（本來算 {was}）")
        rec.row("transactions", t.id, before,
                changelog.snapshot_row(t, ["category", "project", "reimbursable", "status"]))
    await s.commit()
    rec.says(f"{len(moved)} 筆共 {_money(total)} 移到「{project}」，改成工作支出，"
             f"不再從每天能花的錢裡扣"
             + ("，標成可以報帳。" if reimbursable else "。")
             + " " + "；".join(moved[:4]))
    return {"ok": True, "summary": rec.summary, "moved": len(moved), "total": round(total, 2)}


async def log_expense(s, rec, amount, merchant, category=None, note=None, date=None,  # noqa: A002
                      project=None, reimbursable=None):
    t = await record.record_charge(s, -abs(float(amount)), merchant, "manual")
    # Spending FOR a job is 工作 whatever it bought. Momo told her about a taxi and a meal a
    # production would pay back and they were filed as 交通雜支 and 食, so they ate her daily
    # allowance — the one thing this treatment exists to prevent.
    if project or reimbursable:
        category = category if categories_is_work(category) else "work"
    if category:
        t.category = category
    if project:
        from . import projects as _PJ
        r = await _PJ.resolve(s, project)
        t.project = r["id"] or _PJ.slug(project)
    if reimbursable is not None:
        t.reimbursable = bool(reimbursable)
    if note:
        t.note = note
    if date:
        try:
            t.posted_at = datetime.fromisoformat(date).replace(tzinfo=TZ)
        except ValueError:
            pass
    if category or note:
        t.status = "enriched"
    await s.commit()
    rec.row("transactions", t.id, None,
            changelog.snapshot_row(t, ["account_id", "amount", "merchant_desc", "posted_at",
                                       "category", "note", "status", "source",
                                       "project", "reimbursable"]))
    rec.says(f"記了一筆支出：{merchant} {_money(amount)}"
             + (f"（{categories.label(category)}）" if category else ""))
    return {"ok": True, "summary": rec.summary, "id": t.id}


async def log_income(s, rec, amount, source, date=None, account=None, note=None):
    """Money already in her hand. Creates the transaction the bank feed never will.

    The pair add_expected_payment + mark_payment_received looks like it does this and does
    the opposite: it writes a row to the waiting list and then removes it, leaving no trace
    anywhere. Momo's $250 cash job went through that path and disappeared."""
    from .models import Transaction as _T
    amt = abs(float(amount))
    when = now()
    if date:
        try:
            when = datetime.fromisoformat(str(date)[:10]).replace(tzinfo=TZ)
        except ValueError:
            pass
    t = _T(id=f"manual:{abs(hash((source, amt, str(date), when.isoformat()))):016x}",
           account_id=(account or "現金"), amount=amt, merchant_desc=source,
           posted_at=when, status="income", inflow_kind="pay", source="manual",
           note=note or "現金收入，銀行看不到")
    s.add(t)
    await s.commit()
    rec.row("transactions", t.id, None,
            changelog.snapshot_row(t, ["account_id", "amount", "merchant_desc", "posted_at",
                                       "category", "note", "status", "source", "inflow_kind"]))
    line = f"記了一筆收入：{source} {_money(amt)}（{when.date()}）"
    # Cash does not turn up in a balance by itself, and telling her "now go and also update
    # your cash total" is a second chore she will not do. If she said where it went, move it.
    if account:
        accts = prefs._load_list(await get_kv(s, "cfg_accounts"))
        old = next((a for a in accts if prefs._norm(a.get("name")) == prefs._norm(account)), None)
        base = float(old.get("amount") or 0) if old else 0.0
        res = await prefs.update_account(s, account, base + amt,
                                         (old or {}).get("type") or "cash")
        line += f"，{res['name']} {_money(base)} → {_money(base + amt)}"
    else:
        line += "（沒說收在哪個帳戶，餘額沒動）"
    rec.says(line)
    return {"ok": True, "summary": rec.summary, "id": t.id}


async def remember_merchant(s, rec, merchant, category=None, kind=None, note=None):
    key = categories.merchant_key(merchant)
    mem = await s.get(MerchantMemory, key)
    before = (changelog.snapshot_row(mem, ["category", "note", "is_income", "necessary"])
              if mem else None)
    if mem is None:
        mem = MerchantMemory(key=key)
        s.add(mem)
    if category:
        mem.category = category
    if note:
        mem.note = note
    if kind:
        mem.is_income = True if kind == "income" else False if kind == "transfer" else None
    mem.updated_at = now()
    await s.commit()
    rec.row("merchant_memory", key, before,
            changelog.snapshot_row(mem, ["category", "note", "is_income", "necessary"]))
    rec.says(f"記住了：{merchant} 之後算「{category or (kind or '')}」")
    return {"ok": True, "summary": rec.summary}


async def answer_pending_charges(s, rec, reply):
    from . import enrichment
    out = await enrichment.apply_reply(s, reply)
    if not out.get("ok"):
        return {"ok": False, "summary": "", **out}
    cols = ["category", "note", "status", "inflow_kind"]
    for tid, prior in (out.get("before") or {}).items():
        t = await s.get(Transaction, tid)
        if t is not None:
            rec.row("transactions", tid, prior, changelog.snapshot_row(t, cols))
    named = "、".join(f"{i['merchant']} {_money(abs(i['amount']))}"
                      f"（{categories.label(i['category']) if i.get('category') else '未分類'}）"
                      for i in out["items"][:4])
    more = f" 等 {out['n']} 筆" if out["n"] > 4 else ""
    rec.says(f"歸好了 {out['n']} 筆帳：{named}{more}")
    return {"ok": True, "summary": rec.summary, "items": out["items"]}


async def start_new_season(s, rec):
    from . import analytics as AN, season as SE
    te = await AN.to_earn(s, 3)
    old = await SE.get(s)
    new = await SE.start(s, te["tiers"])
    hold = new["targets"].get("持平")
    rec.says(f"這一季重新開始算：{new['start']} 到 {new['end']}，"
             f"持平目標 {_money(hold)}"
             + (f"（上一季是 {old['start']} 開始的）" if old else ""))
    return {"ok": True, "summary": rec.summary, "season": new}


HANDLERS = {
    "answer_pending_charges": answer_pending_charges,
    "start_new_season": start_new_season,
    "add_expected_payment": add_expected_payment,
    "update_expected_payment": update_expected_payment,
    "mark_payment_received": mark_payment_received,
    "remove_expected_payment": remove_expected_payment,
    "set_account_balance": set_account_balance,
    "add_fixed_cost": add_fixed_cost,
    "update_fixed_cost": update_fixed_cost,
    "remove_fixed_cost": remove_fixed_cost,
    "set_savings_plan": set_savings_plan,
    "set_emergency_target": set_emergency_target,
    "set_defend_rung": set_defend_rung,
    "raise_daily": raise_daily,
    "set_day_rate": set_day_rate,
    "close_session": close_session,
    "set_income_baseline": set_income_baseline,
    "log_expense": log_expense,
    "tag_project": tag_project,
    "log_income": log_income,
    "remember_merchant": remember_merchant,
}


async def run(session, name: str, args: dict, *, source_text: str | None = None,
              actor: str = "line") -> dict:
    """Call one tool, log whatever it moved, and hand back a result the model can read.

    A failure is returned, never raised: she has to be told the truth ("找不到那筆") rather
    than the loop dying and her improvising."""
    fn = HANDLERS.get(name)
    if fn is None:
        return {"ok": False, "error": f"沒有 {name} 這個工具"}
    async with changelog.watching(session, tool=name, args=args,
                                  source_text=source_text, actor=actor) as rec:
        try:
            return await fn(session, rec, **args)
        except TypeError as e:
            return {"ok": False, "error": f"參數不對：{e}"}
        except Exception as e:                     # noqa: BLE001 — the model must hear this
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

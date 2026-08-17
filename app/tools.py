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


# ── finding the charges she means ────────────────────────────────────
# 「Uber 多算了兩筆，只有 10.94 那筆是這個工作的 其他是日常開銷」. tag_project had searched
# thirty days of merchant names AND notes for "uber", found four, and moved all four without
# asking — right for Mission Fuel by luck, wrong for the merchant she uses twice a week.
# Any merchant she uses often will over-collect, so the rule is now: one hit writes, more
# than one comes back as a numbered list and she says which.

def _as_amount(v):
    try:
        return float(str(v).replace("$", "").replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _when(t):
    return t.posted_at or t.created_at


def _find_charges(rows, which, cutoff, *, only=None, project=None):
    """Spends matching a merchant fragment or an exact amount, newest first."""
    key = (which or "").strip().lower()
    want = _as_amount(key)
    picks = [a for a in (_as_amount(x) for x in (only or [])) if a is not None]
    hits = []
    for t in rows:
        if t.amount >= 0:
            continue
        d = _when(t)
        if d and d.date() < cutoff:
            continue
        if project is not None and (t.project or "") != project:
            continue
        hay = f"{t.merchant_desc or ''} {t.note or ''}".lower()
        amt = abs(t.amount)
        if not ((key and key in hay) or (want is not None and abs(amt - want) < 0.02)):
            continue
        if picks and not any(abs(amt - p) < 0.02 for p in picks):
            continue
        hits.append(t)
    hits.sort(key=lambda t: (_when(t) is not None, _when(t)), reverse=True)
    return hits, bool(picks)


def _needs_pick(hits, picked, confirm) -> bool:
    """She has to choose unless she already narrowed it herself."""
    return len(hits) > 1 and not picked and not bool(confirm)


def _picker(hits, verb: str) -> dict:
    from . import taxonomy as _T
    rows = [{"n": i, "merchant": (t.merchant_desc or "")[:24],
             "amount": round(abs(t.amount), 2),
             "date": _when(t).date().isoformat() if _when(t) else None,
             "category": _T.label(t.category), "project": t.project or None}
            for i, t in enumerate(hits, 1)]
    listed = "；".join(
        f"{r['n']}. {r['date'] or ''} {r['merchant']} {_money(r['amount'])}"
        f"（{r['category']}）" for r in rows)
    return {
        "ok": False, "needs_pick": rows,
        "error": (f"找到 {len(hits)} 筆，不確定要{verb}哪幾筆，所以還沒動：{listed}。"
                  f"把這幾筆唸給他聽，問是哪幾筆。他報金額之後再呼叫一次，"
                  f"金額放進 only（例如 only=[10.94]）；他說「全部」「都要」就把 confirm 設成 true。"),
    }


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
        "description": (
            "The money finally landed. Finds the matching deposit in the ledger and takes "
            "the invoice off the waiting list. If no deposit is found it will ask WHERE "
            "the money landed rather than guessing — relay that question to her. If she "
            "says it went somewhere the bank cannot see (現金, Venmo), call again with "
            "account set and the income is recorded once."),
        "input_schema": {
            "type": "object",
            "properties": {
                "which": {"type": "string"},
                "account": {"type": "string",
                            "description": "Only when she says the money landed somewhere "
                                           "the bank does not sync — 現金, Venmo, Apple. "
                                           "Never guess this; it creates a transaction."},
            },
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
                "category": {"type": "string", "enum": "__CATS__",
                             "description": "One of the taxonomy ids only."},
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
        "name": "set_project_kind",
        "description": (
            "Say what a job IS: shoot = paid work, portfolio = unpaid / for her reel, "
            "design = paid but not per shoot day, spec = pitch or test. ONLY 'shoot' feeds "
            "her day rate — a portfolio short with four days and no fee would drag the "
            "average down and quietly lower every earning goal that divides by it. Ask "
            "when she mentions a job is unpaid or for her portfolio."),
        "input_schema": {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "kind": {"type": "string", "enum": ["shoot", "portfolio", "design", "spec"]},
            },
            "required": ["project", "kind"],
        },
    },
    {
        "name": "set_claim",
        "description": (
            "Move money she FRONTED along its life: 'todo' = she has not asked for it back "
            "yet, 'sent' = she has asked and is waiting, 'paid' = it came back, 'wont' = "
            "she has decided to eat it. Use it when she says she submitted an expense "
            "report, chased a refund, got money back, or gave up on one. This is what "
            "lets you tell her whether SHE is the one holding it up or they are."),
        "input_schema": {
            "type": "object",
            "properties": {
                "which": {"type": "string", "description": "Merchant, amount, or project."},
                "state": {"type": "string", "enum": ["todo", "sent", "paid", "wont"]},
                "days": {"type": "integer", "description": "How far back to look. Default 90."},
                "only": {"type": "array", "items": {"type": "number"},
                         "description": "The exact amounts she picked, when more than one "
                                        "charge matched and you read her the list."},
                "confirm": {"type": "boolean",
                            "description": "Set true only after she says 全部／都要 to a list "
                                           "you read her. Never set it on the first call."},
            },
            "required": ["which", "state"],
        },
    },
    {
        "name": "match_refunds",
        "description": (
            "Pair credits that have arrived with the costs they repay, by amount. Run it "
            "when a refund or reimbursement lands, or when she asks what is still "
            "outstanding. It only settles where exactly ONE outstanding claim has that "
            "amount; anything ambiguous comes back in `ask` for you to check with her — "
            "two identical fares on one shoot cannot be told apart by the number alone."),
        "input_schema": {"type": "object", "properties": {}},
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
                "only": {"type": "array", "items": {"type": "number"},
                         "description": "The exact amounts she picked, when more than one "
                                        "charge matched and you read her the list."},
                "confirm": {"type": "boolean",
                            "description": "Set true only after she says 全部／都要 to a list "
                                           "you read her. Never set it on the first call."},
            },
            "required": ["which", "project"],
        },
    },
    {
        "name": "untag_project",
        "description": (
            "Take charges back OFF a job — she says something you filed under a project "
            "was actually ordinary spending. This is the correction path for tag_project, "
            "including a partial one: 「只有 10.94 那筆是這個工作的，其他是日常開銷」 means "
            "untag_project on the rest. Their old categories come back from the log, and "
            "they start counting against her daily allowance again.\n"
            "Use this and NOT tag_project whenever the direction is out. If she is picking "
            "which of several charges to keep on the job, untag the ones she did NOT name."),
        "input_schema": {
            "type": "object",
            "properties": {
                "which": {"type": "string",
                          "description": "Merchant name or exact amount to find the charges by."},
                "project": {"type": "string",
                            "description": "Only look inside this job, if she named one."},
                "days": {"type": "integer", "description": "How far back to look. Default 60."},
                "only": {"type": "array", "items": {"type": "number"},
                         "description": "The exact amounts she picked, when more than one "
                                        "charge matched and you read her the list."},
                "confirm": {"type": "boolean",
                            "description": "Set true only after she says 全部／都要 to a list "
                                           "you read her. Never set it on the first call."},
            },
            "required": ["which"],
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
                "category": {"type": "string", "enum": "__CATS__",
                             "description": "One of the taxonomy ids only."},
                "kind": {"type": "string", "enum": ["spend", "income", "transfer"]},
                "note": {"type": "string"},
            },
            "required": ["merchant"],
        },
    },
    {
        "name": "rehearse",
        "description": (
            "Run a scheduled message for rehearsal and report it to the 機房 (control "
            "room) — nothing is sent to Momo and nothing is written. Use when she says "
            "「彩排一下」「測試結算訊息」「跑一次看看」. kind: boundary = the 結算/開期 "
            "message, backup = the weekly backup nudge."),
        "input_schema": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": ["all", "boundary", "backup"],
                         "description": "Default all — runs both and reports both links."},
            },
        },
    },
    {
        "name": "start_settlement",
        "description": (
            "Give Momo the link to the settlement page when she asks to close a period or "
            "a season, or asks to see one early. Use for 「我要結算」「開結算」「這一季來收尾」"
            "「給我結算連結」. If nothing is actually due it returns a rehearsal link, which "
            "is safe — a preview writes nothing. Never claim a settlement happened; only "
            "the page can do that."),
        "input_schema": {
            "type": "object",
            "properties": {
                "scope": {"type": "string", "enum": ["session", "quarter"],
                          "description": "Which one she means. Default session."},
            },
        },
    },
    {
        "name": "jar_create",
        "description": (
            "Open a NEW jar when Momo says she wants to save toward something — a trip, "
            "a lens, a course, a deposit. A jar is a declared intention, NOT a bill: it "
            "opens at $0, changes nothing about her spending allowance, and only holds "
            "money she deliberately allocates at a settlement. If she names a date, pass "
            "by_date; the tool reports what that implies per period as ADVICE. Do not "
            "invent a target she didn't say — ask her instead."),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "What she calls it, e.g. 台灣旅費."},
                "target": {"type": "number", "description": "Total she wants in it, if she said."},
                "by_date": {"type": "string", "description": "YYYY-MM-DD, if she named a deadline."},
                "purpose": {"type": "string", "description": "One line, in her words."},
            },
            "required": ["name"],
        },
    },
    {
        "name": "jar_remove",
        "description": "Delete an empty jar she no longer wants. Refuses if it holds money.",
        "input_schema": {
            "type": "object",
            "properties": {"jar": {"type": "string"}},
            "required": ["jar"],
        },
    },
    {
        "name": "jar_allocate",
        "description": (
            "Put money INTO one of the jars (有主的錢): 短期應急 / 地板 / 緊急預備金 / "
            "DMV / 修車 / 季目標 / 實驗. Use when she allocates surplus — at 期末結算 or "
            "any time she says so. Money in a jar is spoken for and leaves the spending "
            "water."),
        "input_schema": {
            "type": "object",
            "properties": {
                "jar": {"type": "string", "description": "Which jar, by name — e.g. 應急, 地板, 預備金, 實驗."},
                "amount": {"type": "number"},
            },
            "required": ["jar", "amount"],
        },
    },
    {
        "name": "jar_draw",
        "description": (
            "Take money OUT of a jar. Rules are enforced server-side: 應急/實驗/季目標 open "
            "anytime; 地板 only when the current period is genuinely underwater on timing "
            "(money is on the way); 緊急預備金 only for a structural income stop AND with a "
            "stated plan (per-period amount × periods) in `plan`. 稅 never. If refused, "
            "relay the reason honestly — do not look for a way around it."),
        "input_schema": {
            "type": "object",
            "properties": {
                "jar": {"type": "string"},
                "amount": {"type": "number"},
                "plan": {"type": "string",
                         "description": "For 緊急預備金 only: 每期拿多少、撐幾期."},
            },
            "required": ["jar", "amount"],
        },
    },
    {
        "name": "jar_set",
        "description": (
            "Change a jar's target, or a sinking jar's yearly amount (annual). "
            "Does not move any money."),
        "input_schema": {
            "type": "object",
            "properties": {
                "jar": {"type": "string"},
                "target": {"type": "number"},
                "annual": {"type": "number",
                           "description": "For DMV/修車: the yearly cost the drip works toward."},
                "by_date": {"type": "string",
                            "description": "YYYY-MM-DD deadline for a goal jar; empty string clears it."},
            },
            "required": ["jar"],
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
    # the rate note — at booking time, because that is the only moment it is actionable
    if days:
        from . import watch as W
        note_r = await W.rate_note(s, amount, days)
        if note_r:
            rec.says(rec.summary + "　" + note_r)
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
    if extra and hit is None:
        # the extras were NOT written (no row matched by then) — saying 「拍 8 天」 while
        # writing nothing is the exact lie the tools exist to make impossible
        return {"ok": False, "error": f"找到「{before.get('note')}」但補充欄位沒寫進去，"
                                      "再試一次或跟我說原話。"}
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


async def mark_payment_received(s, rec, which, account=None):
    """Clear a pending invoice — but never let the money vanish.

    The old version flipped a flag and stopped. For a Zelle into Chase that was CORRECT:
    the bank feed records the deposit, and writing a second transaction would double-count
    the job (seed_invoices exists precisely to avoid that). But for cash or Venmo the feed
    never sees it, so 「入帳了」 removed the invoice and recorded the money nowhere —
    $250 of real income once evaporated exactly this way.

    So: look for the deposit in the ledger first. Found → link it and clear the invoice.
    Not found and she named a non-synced account → record the income (once). Not found and
    no account → REFUSE and ask where it landed, because both guesses are expensive:
    assuming the bank saw it loses the money, assuming it didn't counts it twice."""
    from sqlalchemy import select as _sel
    items = prefs._load_list(await get_kv(s, "cfg_upcoming"))
    old, amb = _resolve(items, which, "note")
    if old is None:
        return _miss(which, items, "note", amb, "待收款")
    amt = float(old.get("amount") or 0)

    # the deposit, if the feed already caught it: a positive row near the amount,
    # recent, not already spoken for by another invoice
    from .config import aware as _aware
    rows = (await s.execute(_sel(Transaction).where(Transaction.amount > 0))).scalars().all()
    cutoff = now() - __import__("datetime").timedelta(days=60)
    dep = None
    for t in sorted(rows, key=lambda t: _aware(t.posted_at or t.created_at) or now(),
                    reverse=True):
        d = _aware(t.posted_at or t.created_at)
        if d is None or d < cutoff:
            continue
        if abs(t.amount - amt) > max(1.0, amt * 0.02):
            continue
        if (t.note or "").startswith("對上待收款"):
            continue
        dep = t
        break

    if dep is not None:
        before = changelog.snapshot_row(dep, ["status", "inflow_kind", "note"])
        if dep.status != "enriched":
            dep.status = "income"
        dep.inflow_kind = dep.inflow_kind or "pay"
        dep.note = (dep.note or "") or f"對上待收款「{old.get('note')}」"
        await s.commit()
        rec.row("transactions", dep.id, before,
                changelog.snapshot_row(dep, ["status", "inflow_kind", "note"]))
        hit = await prefs.mark_invoice(s, which, "received")
        dd = _aware(dep.posted_at or dep.created_at)
        rec.says(f"「{old.get('note')}」{_money(amt)} 入帳了——就是 {dd.date()} 進來的 "
                 f"{_money(dep.amount)}（{(dep.merchant_desc or '')[:24]}），從待收款劃掉")
        return {"ok": True, "summary": rec.summary, "item": hit, "matched_txn": dep.id}

    if account:
        # a place the bank can't see — record the income exactly once, then clear
        out = await log_income(s, rec, amount=amt, source=old.get("note") or which,
                               account=account, note=f"待收款入帳（{account}）")
        if not out.get("ok"):
            return out
        hit = await prefs.mark_invoice(s, which, "received")
        rec.says(f"「{old.get('note')}」{_money(amt)} 收進{account}了，記了收入、從待收款劃掉")
        return {"ok": True, "summary": rec.summary, "item": hit}

    return {"ok": False, "needs_confirm": True,
            "error": (f"帳上最近 60 天找不到 {_money(amt)} 上下的入帳。先問他錢進到哪裡："
                      "銀行（Chase）的話等下一次同步就會看到，先不要劃掉；"
                      "現金／Venmo 那種銀行看不到的，跟我說帳戶名稱再呼叫一次（account=...），"
                      "我會把收入記起來。不確定就先不要動——劃掉又沒記到，錢就不見了。")}


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

    amount = round(float(amount), 2)
    if amount <= 0:
        # abs() used to turn 「幫我降 $20」 into +$20/day. Lowering the daily is not a
        # grant — she just spends less and the pool grows by itself.
        return {"ok": False, "error": "加碼要是正數。想過得省一點不用叫我，"
                                      "少花的自己會進本期口袋。"}
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
        from . import jars as JARS
        if await JARS.seeded(s):
            out = await JARS.allocate(s, "season", pool)
            rec.says(f"{c['label']} 結算：{c['days_under']} 天守住、{c['days_over']} 天超過，"
                     f"口袋 {_money(pool)} 放進季目標罐，累計 {_money(out['balance'])}。")
        else:
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


async def set_project_kind(s, rec, project, kind):
    """Mark what a job IS. Unpaid work stays in the record and out of the day rate."""
    from . import projects as PJ
    if kind not in PJ.KINDS:
        return {"ok": False, "error": f"kind 只能是 {'／'.join(PJ.KINDS)}"}
    hit = await PJ.resolve(s, project)
    if hit["id"] is None:
        names = "、".join(o["name"] for o in hit["options"][:4])
        return {"ok": False, "error": f"「{project}」對得上好幾個案子：{names}。是哪一個？"}
    await PJ.annotate(s, hit["id"], kind=kind)
    zh = PJ.KINDS[kind]
    extra = ("　（無酬的案子不會算進日薪，也不會拉低要接多少的估算）"
             if kind not in PJ.RATED else "")
    rec.says(f"「{project}」標成{zh}。{extra}")
    return {"ok": True, "summary": rec.summary, "id": hit["id"], "kind": kind}


async def set_claim(s, rec, which, state, days=90, only=None, confirm=False):
    """Move a fronted cost along: 還沒去要 → 已經要了 → 收回來了（或決定自己吸收）。"""
    from sqlalchemy import select
    from . import claims as CL
    from datetime import timedelta as _td

    if state not in ("todo", "sent", "paid", "wont"):
        return {"ok": False, "error": "state 只能是 todo／sent／paid／wont"}
    if not (which or "").strip():
        return {"ok": False, "error": "要說是哪一筆（商家名字或金額）"}
    cutoff = now().date() - _td(days=int(days or 90))
    rows = (await s.execute(select(Transaction))).scalars().all()
    hits, picked = _find_charges(rows, which, cutoff, only=only)
    if not hits:
        return {"ok": False, "error": f"最近 {days} 天找不到「{which}」。"}
    if _needs_pick(hits, picked, confirm):
        return _picker(hits, f"改成「{CL.LABEL[state]}」的")
    total = 0.0
    for t in hits:
        before = changelog.snapshot_row(t, ["claim", "reimbursable"])
        t.claim = state
        if state in ("todo", "sent") and t.reimbursable is None:
            t.reimbursable = True
        total += abs(t.amount)
        rec.row("transactions", t.id, before, changelog.snapshot_row(t, ["claim", "reimbursable"]))
    await s.commit()
    rec.says(f"{len(hits)} 筆共 {_money(total)} → {CL.LABEL[state]}")
    return {"ok": True, "summary": rec.summary, "n": len(hits), "total": round(total, 2)}


async def match_refunds(s, rec, apply=True):
    """Pair credits that came in with the costs they repay, and hand back what is unclear."""
    from . import claims as CL
    out = await CL.match(s, apply=bool(apply))
    for ch in out.pop("changes", []):
        rec.row(ch["table"], ch["id"], ch["before"], ch["after"])
    if out["n_settled"]:
        rec.says(f"對上 {out['n_settled']} 筆退款／報帳："
                 + "；".join(f"{x['merchant']} {_money(x['amount'])}" for x in out["settled"][:4]))
    return {"ok": True, "summary": rec.summary or "沒有可以自動對上的。", **out}


async def tag_project(s, rec, which, project, reimbursable=None, days=30,
                      only=None, confirm=False):
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
    hits, picked = _find_charges(rows, which, cutoff, only=only)
    if not hits:
        return {"ok": False, "error": f"最近 {days} 天找不到「{which}」這筆。"}
    if _needs_pick(hits, picked, confirm):
        return _picker(hits, "移到案子裡的")

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


async def untag_project(s, rec, which, project=None, days=60, only=None, confirm=False):
    """Take charges back off a job and return them to her ordinary spending.

    The missing half. `tag_project` could only move money in, so 「Uber 多算了兩筆，只有
    10.94 那筆是這個工作的」 was a sentence with no tool behind it — 陳會計 had a clear
    instruction, a rule saying she may only change data by calling something, and nothing
    to call. She went silent, and silence repeated on retry because the gap is structural.

    Putting a charge back needs its old category, and guessing would quietly rewrite her
    history. So the log is asked first: every tool write photographs the row before it
    touches it, which makes 「what was this before I moved it」 a lookup, not an estimate.
    Merchant memory, then the classifier's guess, only cover rows no tool ever touched.
    """
    from sqlalchemy import select
    from . import taxonomy as T
    from . import projects as PJ
    from datetime import timedelta as _td

    if not (which or "").strip():
        return {"ok": False, "error": "要說是哪一筆（商家名字或金額）"}
    pid = None
    if project:
        hit = await PJ.resolve(s, project)
        if hit["id"] is None:
            names = "、".join(o["name"] for o in hit["options"][:4])
            return {"ok": False, "ambiguous": hit["options"],
                    "error": f"「{project}」對得上好幾個案子：{names}。是哪一個？"}
        pid = hit["id"]
    cutoff = now().date() - _td(days=int(days or 60))

    rows = (await s.execute(select(Transaction))).scalars().all()
    hits, picked = _find_charges(rows, which, cutoff, only=only, project=pid)
    hits = [t for t in hits if t.project or T.is_work(t.category)]
    if not hits:
        return {"ok": False,
                "error": f"最近 {days} 天沒有掛在案子上的「{which}」，本來就是日常開銷了。"}
    if _needs_pick(hits, picked, confirm):
        return _picker(hits, "從案子裡拿掉的")

    cols = ["category", "project", "reimbursable", "status", "claim"]
    back, total, guessed = [], 0.0, 0
    for t in hits:
        before = changelog.snapshot_row(t, cols)
        prior = await changelog.prior_value(s, "transactions", t.id, "category")
        if prior["found"] and not T.is_work(prior["value"]):
            cat = prior["value"]
        else:
            guessed += 1
            desc = t.merchant_desc or ""
            mem = await s.get(MerchantMemory, categories.merchant_key(desc))
            cat = (mem.category if mem is not None and mem.category
                   else categories.guess(desc))
        was = t.project
        t.category = cat
        t.project = None
        t.reimbursable = False
        t.claim = None
        total += abs(t.amount)
        back.append(f"{(t.merchant_desc or '')[:18]} {_money(abs(t.amount))}"
                    f"（{('離開「' + was + '」，') if was else ''}改回 {T.label(cat)}）")
        rec.row("transactions", t.id, before, changelog.snapshot_row(t, cols))
    await s.commit()
    rec.says(f"{len(back)} 筆共 {_money(total)} 從案子裡拿掉，改回日常開銷，"
             f"會重新從每天能花的錢裡扣。 " + "；".join(back[:4])
             + (f"（其中 {guessed} 筆找不到原本的分類，用商家猜的，不對再跟我講）"
                if guessed else ""))
    return {"ok": True, "summary": rec.summary, "moved": len(back),
            "total": round(total, 2), "guessed": guessed}


async def log_expense(s, rec, amount, merchant, category=None, note=None, date=None,  # noqa: A002
                      project=None, reimbursable=None):
    if not _valid_category(category):
        return {"ok": False, "error": f"沒有「{category}」這個分類。留空讓我猜，或用正式的分類 id。"}
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
    import uuid as _uuid
    t = _T(id=f"manual:{_uuid.uuid4().hex[:16]}",
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
        kind = (old or {}).get("type") or "cash"
        if kind in ("debt", "card", "credit"):
            # 「錢進了 Apple Card」 is a payment toward the card, not a deposit — ADDING it
            # to a debt balance would record getting paid as owing more
            line += f"，{account} 是卡債帳戶，餘額我沒動——那要用還卡費的方式記"
        else:
            base = float(old.get("amount") or 0) if old else 0.0
            res = await prefs.update_account(s, account, base + amt, kind)
            line += f"，{res['name']} {_money(base)} → {_money(base + amt)}"
    else:
        line += "（沒說收在哪個帳戶，餘額沒動）"
    rec.says(line)
    return {"ok": True, "summary": rec.summary, "id": t.id}


async def remember_merchant(s, rec, merchant, category=None, kind=None, note=None):
    if not _valid_category(category):
        return {"ok": False, "error": f"沒有「{category}」這個分類。"}
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


# An invalid category id makes treatment() return None — before B-5 that meant the
# spend silently stopped counting against the allowance, with a cheerful receipt. The
# enum stops the model inventing ids; _valid_category stops everything else.
def _valid_category(cat):
    from . import taxonomy as _T
    return cat is None or cat in _T.ALL


def _install_category_enums():
    from . import taxonomy as _T
    cats = sorted(_T.ALL)
    for sch in SCHEMAS:
        props = sch.get("input_schema", {}).get("properties", {})
        c = props.get("category")
        if c and c.get("enum") == "__CATS__":
            c["enum"] = cats


_install_category_enums()


# ── 有主的錢 ──────────────────────────────────────────────────────────
_JAR_ALIASES: dict[str, tuple[str, ...]] = {
    "contingency": ("短期應急", "應急", "急用"),
    "floor": ("地板",),
    "emergency": ("緊急預備金", "預備金", "備用金", "救命錢"),
    "dmv": ("dmv", "牌照", "規費"),
    "car": ("修車", "車子"),
    "season": ("季目標", "這一季", "季"),
    "experiment": ("實驗", "ai", "實驗罐", "學ai"),
    "tax": ("稅", "tax"),
}


def _jar_resolve(which: str, stored: list[dict] | None = None) -> tuple[str | None, list[str]]:
    """Which jar does she mean? Ambiguity returns candidates and writes nothing (C-8).

    The STORED jars are the source of truth — a jar Momo made herself has to be
    addressable by its own name, which a hard-coded alias table can never know about.
    The table survives only as extra nicknames for the seeded seven.
    """
    q = (which or "").strip().lower()
    if not q:
        return None, []
    names: dict[str, set[str]] = {}
    for j in stored or []:
        jid = j.get("id")
        if not jid:
            continue
        names.setdefault(jid, set()).update(
            {jid.lower(), (j.get("name") or "").strip().lower()} - {""})
    for jid, extra in _JAR_ALIASES.items():
        if stored is None or jid in names or jid == "tax":
            names.setdefault(jid, set()).update({jid.lower(), *extra})
    if q in names:
        return q, []
    exact = [jid for jid, ns in names.items() if q in ns]
    if len(exact) == 1:
        return exact[0], []
    hits = [jid for jid, ns in names.items()
            if any(q == n or q in n or n in q for n in ns)]
    if len(hits) == 1:
        return hits[0], []
    return None, sorted(set(exact or hits))


_JAR_NAME = {"contingency": "短期應急", "floor": "地板", "emergency": "緊急預備金",
             "dmv": "DMV", "car": "修車", "season": "季目標", "experiment": "實驗（AI）",
             "tax": "預留的稅"}


def _jar_pick_error(which: str, hits: list[str], stored: list[dict] | None = None) -> dict:
    look = {j.get("id"): (j.get("name") or j.get("id")) for j in (stored or [])}
    nm = lambda k: look.get(k) or _JAR_NAME.get(k) or k          # noqa: E731
    if hits:
        opts = "\n".join(f"{i + 1}. {nm(h)}" for i, h in enumerate(hits))
        return {"ok": False, "needs_pick": True,
                "error": f"「{which}」可能是這幾罐，問她是哪一個，先不要動：\n{opts}"}
    have = [nm(j.get("id")) for j in (stored or [])] or [
        _JAR_NAME[k] for k in _JAR_ALIASES if k != "tax"]
    return {"ok": False, "error": f"沒有叫「{which}」的罐子。現有的：{'、'.join(have)}。"}


async def h_jar_allocate(s, rec, jar: str, amount: float):
    from . import jars as JR
    stored = await JR.load(s)
    jid, hits = _jar_resolve(jar, stored)
    if jid is None:
        return _jar_pick_error(jar, hits, stored)
    out = await JR.allocate(s, jid, float(amount))
    if out.get("ok"):
        rec.says(out["receipt"])
    return out


async def h_jar_draw(s, rec, jar: str, amount: float, plan: str | None = None):
    from . import allowance as _AL
    from . import jars as JR
    stored = await JR.load(s)
    jid, hits = _jar_resolve(jar, stored)
    if jid is None:
        return _jar_pick_error(jar, hits, stored)
    # the dip context is the ENGINE's, not the model's — the caller can't talk her way
    # into a bridge draw by describing the situation generously
    ctx = await _AL.compute(s)
    out = await JR.draw(s, jid, float(amount), deficit_kind=ctx.get("deficit_kind"),
                        dip_active=bool(ctx.get("deficit")), plan=plan)
    if out.get("ok"):
        rec.says(out["receipt"])
    return out


async def h_jar_set(s, rec, jar: str, target: float | None = None,
                    annual: float | None = None, by_date: str | None = None):
    from . import jars as JR
    stored = await JR.load(s)
    jid, hits = _jar_resolve(jar, stored)
    if jid is None:
        return _jar_pick_error(jar, hits, stored)
    if jid == "tax":
        return {"ok": False, "error": "稅的目標是算出來的，不是設定的。"}
    js = await JR.load(s)
    j = JR.get(js, jid)
    if j is None:
        return {"ok": False, "error": "罐子還沒建——先去訓練輪按一次重掃歷史。"}
    said = []
    if target is not None:
        j["target"] = round(float(target), 2)
        said.append(f"目標 ${j['target']:,.2f}")
    if annual is not None and j.get("fill") == "drip":
        j["annual"] = round(float(annual), 2)
        said.append(f"一年 ${j['annual']:,.2f}（每期滴 ${j['annual'] / 24:,.2f}）")
    if by_date is not None:
        j["by_date"] = by_date or None
        adv = JR.rate_advice(j)
        said.append(f"期限 {by_date}" + (f"（{adv['text']}）" if adv else ""))
    if not said:
        return {"ok": False, "error": "要改什麼？target 或 annual 至少給一個。"}
    await JR.save(s, js)
    rec.says(f"{j['name']}：" + "、".join(said))
    return {"ok": True, "jar": j["name"]}


async def h_jar_create(s, rec, name: str, target: float | None = None,
                       by_date: str | None = None, purpose: str | None = None):
    from . import jars as JR
    out = await JR.create(s, name, target=target, by_date=by_date, purpose=purpose)
    if out.get("ok"):
        rec.says(out["receipt"])
        adv = JR.rate_advice(out["jar"])
        if adv:
            out["advice"] = adv["text"]
    return out


async def h_jar_remove(s, rec, jar: str):
    from . import jars as JR
    stored = await JR.load(s)
    jid, hits = _jar_resolve(jar, stored)
    if jid is None:
        return _jar_pick_error(jar, hits, stored)
    if jid in ("tax", "floor", "emergency", "contingency", "season"):
        return {"ok": False, "error": "這是基本的罐子，不能刪。目標設 0 就好。"}
    out = await JR.remove(s, jid)
    if out.get("ok"):
        rec.says(out["receipt"])
    return out


async def h_start_settlement(s, rec, scope: str = "session"):
    from .config import public_url
    from . import settle as ST
    base = public_url() or ""
    st = await ST.state(s)
    if scope == "quarter":
        q = await ST.quarter_pending(s)
        if q:
            return {"ok": True, "due": True,
                    "reply": f"這一季（{q['start']}~{q['end']}）可以結算了：{base}/settle"}
        return {"ok": True, "due": False,
                "reply": ("這一季還沒結束，先給你看預覽（按了不會寫進去）："
                          f"{base}/settle?preview=quarter")}
    if st["awaiting"]:
        return {"ok": True, "due": True,
                "reply": f"{st['label']} 還沒結算，這就是連結：{base}/settle"}
    return {"ok": True, "due": False,
            "reply": ("現在沒有要結算的期。想先看看長怎樣的話，這是預覽（不會寫進去）："
                      f"{base}/settle?preview=session")}


async def h_rehearse(s, rec, kind: str = "all"):
    from . import main as M
    text = await M.rehearse(kind)
    rec.says(f"彩排跑完了（{kind}），結果送到機房，沒有寄給你、也沒有寫任何紀錄。")
    return {"ok": True, "rehearsal": text}


HANDLERS = {
    "rehearse": h_rehearse,
    "start_settlement": h_start_settlement,
    "jar_create": h_jar_create,
    "jar_remove": h_jar_remove,
    "jar_allocate": h_jar_allocate,
    "jar_draw": h_jar_draw,
    "jar_set": h_jar_set,
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
    "untag_project": untag_project,
    "set_project_kind": set_project_kind,
    "set_claim": set_claim,
    "match_refunds": match_refunds,
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

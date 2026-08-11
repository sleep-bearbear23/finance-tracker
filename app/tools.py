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
_CAD_ZH = {"monthly": "每月", "quarterly": "每季", "semiannual": "每半年", "annual": "每年"}


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
                         "description": "YYYY-MM she expects it to land. Omit if unknown; "
                                        "do not guess a month she did not imply."},
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
                "when": {"type": "string", "description": "YYYY-MM"},
                "note": {"type": "string"},
            },
            "required": ["which"],
        },
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
                "force": {"type": "boolean",
                          "description": "Only after Momo confirms she really pays both. Adding "
                                         "a second line for a bill she already has silently "
                                         "doubles it in every budget."},
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
            },
            "required": ["amount", "merchant"],
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


async def add_expected_payment(s, rec, amount, note, when=None, force=False):
    # A duplicate here is not a cosmetic problem: 待收款 feeds the projection and the
    # 需要賺 index, so the same job entered twice tells her she can afford things twice.
    if not force:
        dup = _find(prefs._load_list(await get_kv(s, "cfg_upcoming")), note, "note")
        if dup and abs(float(dup.get("amount") or 0) - float(amount)) < 1:
            return {"ok": False, "summary": "", "duplicate": dup,
                    "error": (f"已經有一筆很像的：「{dup.get('note')}」{_money(dup.get('amount'))}"
                              f"（{dup.get('when') or '未定'}）。是要改那一筆，還是真的是另一個案子？"
                              "確定是新的再用 force=true。")}
    item = await prefs.add_invoice(s, amount, when, note)
    rec.says(f"加了一筆待收款：{note} {_money(amount)}"
             + (f"，預計 {when} 入帳" if when else "，還沒說什麼時候"))
    return {"ok": True, "summary": rec.summary, "item": item}


async def update_expected_payment(s, rec, which, amount=None, when=None, note=None):
    items = prefs._load_list(await get_kv(s, "cfg_upcoming"))
    old, amb = _resolve(items, which, "note")
    if old is None:
        return _miss(which, items, "note", amb, "待收款")
    before = dict(old)
    hit = await prefs.update_invoice(s, which, amount, when, note)
    bits = []
    if amount is not None and float(before.get("amount") or 0) != float(amount):
        bits.append(f"{_money(before.get('amount'))} → {_money(amount)}")
    if when and before.get("when") != when:
        bits.append(f"{before.get('when') or '未定'} → {when}")
    if note and before.get("note") != note:
        bits.append(f"備註改成「{note}」")
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
                         next_due=None, where=None, note=None, force=False):
    if cadence not in CADENCES:
        return {"ok": False, "summary": "", "error": f"cadence 只能是 {CADENCES}"}
    rows = await fixed.rows(s, include_sinking=False)
    # 「Claude 訂閱」 added on top of 「Claude 訂閱（含加值）」 quietly charges her twice, every
    # month, in every lens. Refuse and make somebody choose.
    if not force:
        dup = _find(rows, name)
        if dup is not None:
            return {"ok": False, "summary": "", "duplicate": dup,
                    "error": (f"固定開銷裡已經有「{dup.get('name')}」"
                              f"{_CAD_ZH.get(dup.get('cadence'), '')} {_money(dup.get('amount'))}。"
                              "是那一筆漲價了（用 update_fixed_cost），還是真的是另一筆訂閱？"
                              "確定是新的再用 force=true。")}
    rows.append({"name": name, "amount": float(amount), "cadence": cadence,
                 "cat": category, "where": where, "next_due": next_due, "note": note})
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


async def set_income_baseline(s, rec, amount):
    old = await get_kv(s, "cfg_monthly_baseline")
    await set_kv(s, "cfg_monthly_baseline", str(float(amount)))
    rec.says(f"淡月底收入 {_money(old) if old else '未設定'} → 每月 {_money(amount)}")
    return {"ok": True, "summary": rec.summary}


async def log_expense(s, rec, amount, merchant, category=None, note=None, date=None):  # noqa: A002
    t = await record.record_charge(s, -abs(float(amount)), merchant, "manual")
    if category:
        t.category = category
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
                                       "category", "note", "status", "source"]))
    rec.says(f"記了一筆支出：{merchant} {_money(amount)}"
             + (f"（{categories.label(category)}）" if category else ""))
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


HANDLERS = {
    "answer_pending_charges": answer_pending_charges,
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
    "set_income_baseline": set_income_baseline,
    "log_expense": log_expense,
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

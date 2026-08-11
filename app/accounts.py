"""Logical accounts — the spine that connects balances, records and coverage.

Until now the app only knew `source` tags and bank ids, which is why Apple Card showed
up twice (帳單 vs 截圖). An account is the thing Momo actually thinks in: one card or
one bank account, fed by one or more data sources.

  bank:<simplefin id>   Chase accounts — balance synced, transactions synced
  manual:<slug>         Apple Card / Apple GS Savings / Venmo — Momo states the balance
  record:notion         historical income import (a record, not a spendable account)

Every transaction resolves to exactly one logical account, so an account's page and its
balance are always talking about the same money.
"""
from __future__ import annotations

import re

from sqlalchemy import select

from . import prefs
from .config import aware, now
from .models import Account, Transaction

# Sources that are always Momo's Apple Card (statements, nightly screenshots, Apple Pay taps).
_APPLE_SOURCES = {"applecard", "screenshot"}
_CARD_WORDS = ("card", "freedom", "credit", "visa", "mastercard", "amex", "discover")
NOTION_ID = "record:notion"
OTHER_ID = "manual:other"


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-") or "unknown"


def norm(name: str) -> str:
    return re.sub(r"[^a-z0-9一-鿿]+", "", (name or "").lower())


def is_credit(name: str, typ: str | None = None) -> bool:
    if typ:
        return typ == "credit"
    return any(w in norm(name) for w in _CARD_WORDS)


async def registry(session) -> dict[str, dict]:
    """Every logical account Momo has, keyed by id."""
    out: dict[str, dict] = {}

    # 1) bank-synced accounts (Chase via SimpleFIN)
    for a in (await session.execute(select(Account))).scalars().all():
        bal = a.balance or 0.0
        credit = is_credit(a.name) or bal < 0
        out[f"bank:{a.id}"] = {
            "id": f"bank:{a.id}", "name": a.name or a.id, "kind": "credit" if credit else "cash",
            "balance": abs(bal) if credit else bal,     # credit balance = amount owed
            "balance_src": "同步", "org": a.org or "",
            "balance_date": (aware(a.balance_date).date().isoformat() if a.balance_date else None),
            "raw_ids": {a.id},
        }

    # 2) manual ledger accounts (Apple Card, Apple GS Savings, Venmo, cash…)
    prof = await prefs.get_income_profile(session)
    for m in (prof.get("accounts") or []):
        nm = m.get("name") or "帳戶"
        try:
            amt = float(m.get("amount") or 0)
        except (TypeError, ValueError):
            amt = 0.0
        out[f"manual:{slug(nm)}"] = {
            "id": f"manual:{slug(nm)}", "name": nm,
            "kind": "credit" if m.get("type") == "credit" else "cash",
            "balance": amt, "balance_src": "自己報", "org": "", "balance_date": None,
            "raw_ids": set(),
        }
    return out


def _apple_card_id(reg: dict) -> str | None:
    for aid, a in reg.items():
        if aid.startswith("manual:") and "apple" in norm(a["name"]) and a["kind"] == "credit":
            return aid
    return None


def resolver(reg: dict):
    """Build a fast (transaction) -> account_id function for this registry."""
    apple = _apple_card_id(reg)
    by_norm = {norm(a["name"]): aid for aid, a in reg.items()}
    bank_ids = {aid.split("bank:", 1)[1]: aid for aid in reg if aid.startswith("bank:")}

    def resolve(t) -> str:
        src, acct = (t.source or ""), (t.account_id or "")
        if src == "simplefin":
            return bank_ids.get(acct, f"bank:{acct}")
        if src in _APPLE_SOURCES:
            return apple or "manual:apple-card"
        if src == "notion":
            return NOTION_ID
        # Apple Pay tap / manual entry may name the card or account it hit
        n = norm(acct)
        if n:
            if n in by_norm:
                return by_norm[n]
            for k, aid in by_norm.items():
                if len(k) >= 4 and (k in n or n in k):
                    return aid
        if src == "shortcut":
            return apple or "manual:apple-card"
        return OTHER_ID

    return resolve


def placeholder(aid: str) -> dict:
    """A stand-in for transactions whose account isn't in the ledger (imports, strays)."""
    names = {NOTION_ID: "歷史收入紀錄（Notion）", OTHER_ID: "其他 / 手動"}
    return {"id": aid, "name": names.get(aid, aid.split(":", 1)[-1] or aid),
            "kind": "record", "balance": None, "balance_src": "—", "org": "",
            "balance_date": None, "raw_ids": set()}


async def build(session) -> tuple[dict[str, dict], dict[str, list]]:
    """Registry plus each account's transactions, with coverage stats filled in."""
    reg = await registry(session)
    resolve = resolver(reg)
    txns = (await session.execute(select(Transaction))).scalars().all()

    buckets: dict[str, list] = {aid: [] for aid in reg}
    for t in txns:
        aid = resolve(t)
        if aid not in reg:
            reg[aid] = placeholder(aid)
            buckets[aid] = []
        buckets.setdefault(aid, []).append(t)

    today = now().date()
    for aid, a in reg.items():
        rows = buckets.get(aid, [])
        dates = sorted(d.date() for d in (aware(t.posted_at or t.created_at) for t in rows) if d)
        a["n_txns"] = len(rows)
        a["first"] = dates[0].isoformat() if dates else None
        a["last"] = dates[-1].isoformat() if dates else None
        a["stale_days"] = (today - dates[-1]).days if dates else None
        a["sources"] = sorted({t.source for t in rows})
        a.pop("raw_ids", None)
    return reg, buckets


def coverage_note(a: dict) -> str | None:
    """Plain-language warning when an account's records lag behind today."""
    sd = a.get("stale_days")
    if a.get("kind") == "record" or sd is None:
        return None
    if sd >= 7:
        return f"紀錄只到 {a['last']}，已經 {sd} 天沒補了"
    return None

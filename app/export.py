"""A full, redacted snapshot of her internal state — so it can be inspected off-box.

Until now the only way to know what 陳會計 actually thinks was to ask Momo to screenshot
things. That meant every fix was aimed at a guess. This endpoint hands over the whole
picture in one file: the account registry, every transaction, the config she's been told,
what she's learned about merchants, and the allowance with its full reasoning.

Two rules:

* **Secrets never leave.** The SimpleFIN access URL is a bearer credential to Momo's bank
  data and the LINE owner id identifies her account. Both are redacted here, and the
  redaction is a denylist *and* a pattern check, so a new secret key added later doesn't
  quietly start appearing in exports.
* **It's her file.** The export is downloaded by Momo, to Momo's own machine. Nothing is
  pushed anywhere.
"""
from __future__ import annotations

import os
import re

from sqlalchemy import select

from . import accounts as acct
from . import allowance as AL
from . import budget
from . import facts as F
from . import fixed as FX
from . import prefs
from . import tax as TAX
from . import taxonomy as T
from .config import aware, now
from .models import KV, MerchantMemory, Snapshot, Transaction

#: KV keys that must never appear in an export
_SECRET_KEYS = {
    "simplefin_access_url",
    "line_owner_id",
    "owner_id",
    "dashboard_token",
    "ingest_token",
}

#: …and anything that smells like one, so a future key can't slip through
_SECRET_PAT = re.compile(
    r"(token|secret|password|passwd|access_url|api_key|apikey|bearer|credential|"
    r"webhook|_sid|_key$)", re.I)


def _redact(key: str, value: str) -> str | None:
    """Returns the safe value, or None to drop the row entirely."""
    if key.lower() in _SECRET_KEYS or _SECRET_PAT.search(key):
        return None
    # a value that looks like a URL with embedded credentials is a secret regardless of key
    if isinstance(value, str) and re.search(r"https?://[^/\s]*:[^/@\s]+@", value):
        return None
    return value


async def build(session, include_txns: bool = True) -> dict:
    f = await F.build(session)

    kv_rows = (await session.execute(select(KV))).scalars().all()
    config, dropped = {}, []
    for r in kv_rows:
        safe = _redact(r.key, r.value or "")
        if safe is None:
            dropped.append(r.key)
        else:
            config[r.key] = safe

    mem = (await session.execute(select(MerchantMemory))).scalars().all()
    snaps = (await session.execute(select(Snapshot).order_by(Snapshot.day))).scalars().all()

    out: dict = {
        "meta": {
            "generated_at": now().isoformat(),
            "commit": os.environ.get("RAILWAY_GIT_COMMIT_SHA", "")[:7] or None,
            "period": budget.current_key(),
            "redacted_keys": sorted(dropped),
            "note": "秀琴阿姨 internal state. Secrets removed. This file is Momo's.",
        },
        "audit": f.audit(),
        "networth": f.nw,
        "accounts": [
            {**{k: a.get(k) for k in
                ("id", "name", "kind", "balance", "balance_src", "org", "balance_date",
                 "n_txns", "first", "last", "stale_days", "sources", "liquid", "volatile",
                 "runway_value")},
             "coverage_note": acct.coverage_note(a)}
            for a in f.registry.values()
        ],
        "config": config,
        "income_profile": await prefs.get_income_profile(session),
        "prefs": await prefs.get_prefs(session),
        "fixed_costs": await FX.rows(session),
        "sinking_funds": await FX.sinking_rows(session),
        "renewals": await FX.renewals(session, within_days=400),
        "tax": await TAX.status(session),
        "tax_payments_found": await TAX.find_prior_payments(session),
        "merchant_memory": [
            {"key": m.key, "category": m.category, "cat_label": T.label(m.category),
             "note": m.note, "is_income": m.is_income, "necessary": m.necessary,
             "updated_at": aware(m.updated_at).isoformat() if m.updated_at else None}
            for m in mem
        ],
        "snapshots": [
            {"day": s.day, "net_worth": s.net_worth, "assets": s.assets, "debts": s.debts,
             "cash": s.cash, "allowance": s.allowance, "spent": s.spent}
            for s in snaps
        ],
        "category_spend_90d": f.category_spend(90),
        "monthly": f.monthly(12),
    }

    try:
        a = await AL.compute(session)
        a["explain"] = AL.explain(a)
        out["allowance"] = a
    except Exception as e:                      # a broken budget must not block the dump
        out["allowance_error"] = f"{type(e).__name__}: {e}"

    if include_txns:
        rows = (await session.execute(select(Transaction))).scalars().all()
        out["transactions"] = [
            {"id": t.id, "account_id": t.account_id, "amount": t.amount,
             "merchant_desc": t.merchant_desc,
             "posted_at": aware(t.posted_at).isoformat() if t.posted_at else None,
             "effective_at": (aware(t.effective_at).isoformat()
                              if getattr(t, "effective_at", None) else None),
             "category": t.category, "cat_label": T.label(t.category),
             "treatment": T.treatment(t.category),
             "note": t.note, "status": t.status, "source": t.source,
             "inflow_kind": getattr(t, "inflow_kind", None),
             "reimbursable": getattr(t, "reimbursable", None),
             "nets_txn_id": getattr(t, "nets_txn_id", None),
             "created_at": aware(t.created_at).isoformat() if t.created_at else None}
            for t in rows
        ]
        out["meta"]["n_transactions"] = len(rows)
    return out

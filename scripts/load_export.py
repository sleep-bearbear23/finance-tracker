"""Rebuild a local database from a production export, so the real app can run on real data.

    python3 scripts/load_export.py chen-state-20260811-1830.json [--db /tmp/chen.db]

Then point the app at it and everything — the dashboard routes, the allowance, the audit —
runs exactly as it does in production, on the same rows, off-box:

    DATABASE_URL=sqlite+aiosqlite:////tmp/chen.db \
    ANTHROPIC_API_KEY=x LINE_CHANNEL_ACCESS_TOKEN=x LINE_CHANNEL_SECRET=x \
    DASHBOARD_TOKEN=local python3 -m uvicorn app.main:app --port 8080

The export carries no secrets, so this database can't reach Momo's bank or send her a
message. It only reproduces what she sees.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _dt(v):
    if not v:
        return None
    try:
        return datetime.fromisoformat(v)
    except ValueError:
        return None


async def load(path: Path, db: str) -> None:
    os.environ.setdefault("ANTHROPIC_API_KEY", "x")
    os.environ.setdefault("LINE_CHANNEL_ACCESS_TOKEN", "x")
    os.environ.setdefault("LINE_CHANNEL_SECRET", "x")
    os.environ.setdefault("DASHBOARD_TOKEN", "local")
    os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{db}"

    from app import migrate                       # noqa: E402
    from app.db import Session, engine, init_db   # noqa: E402
    from app.models import KV, Account, MerchantMemory, Snapshot, Transaction  # noqa: E402

    data = json.loads(path.read_text(encoding="utf-8"))
    Path(db).unlink(missing_ok=True)
    await init_db()
    await migrate.run(engine)

    async with Session() as s:
        # Bank-synced accounts are reconstructed from the registry's bank:* entries; the
        # manual ones live in config (cfg_accounts) and come back with the KV rows.
        n_acct = 0
        for a in data.get("accounts", []):
            if not str(a.get("id", "")).startswith("bank:"):
                continue
            bal = a.get("balance") or 0.0
            s.add(Account(
                id=a["id"].split("bank:", 1)[1], name=a.get("name") or "",
                org=a.get("org") or "",
                balance=-bal if a.get("kind") == "credit" else bal,
                balance_date=_dt(a.get("balance_date")),
            ))
            n_acct += 1

        for k, v in (data.get("config") or {}).items():
            s.add(KV(key=k, value=str(v)))

        for m in data.get("merchant_memory", []):
            s.add(MerchantMemory(
                key=m["key"], category=m.get("category"), note=m.get("note"),
                is_income=m.get("is_income"), necessary=bool(m.get("necessary")),
                updated_at=_dt(m.get("updated_at")) or datetime.now(),
            ))

        for sn in data.get("snapshots", []):
            s.add(Snapshot(
                day=sn["day"], net_worth=sn.get("net_worth") or 0.0,
                assets=sn.get("assets") or 0.0, debts=sn.get("debts") or 0.0,
                cash=sn.get("cash") or 0.0, allowance=sn.get("allowance") or 0.0,
                spent=sn.get("spent") or 0.0,
            ))

        for t in data.get("transactions", []):
            s.add(Transaction(
                id=t["id"], account_id=t.get("account_id") or "", amount=t["amount"],
                merchant_desc=t.get("merchant_desc") or "",
                posted_at=_dt(t.get("posted_at")), category=t.get("category"),
                note=t.get("note"), status=t.get("status") or "auto",
                source=t.get("source") or "simplefin",
                inflow_kind=t.get("inflow_kind"), reimbursable=t.get("reimbursable"),
                nets_txn_id=t.get("nets_txn_id"), effective_at=_dt(t.get("effective_at")),
                created_at=_dt(t.get("created_at")) or datetime.now(),
            ))
        await s.commit()

    print(f"loaded {db}")
    print(f"  accounts (bank)   {n_acct}")
    print(f"  transactions      {len(data.get('transactions') or [])}")
    print(f"  merchant memory   {len(data.get('merchant_memory') or [])}")
    print(f"  snapshots         {len(data.get('snapshots') or [])}")
    print(f"  config keys       {len(data.get('config') or {})}")
    print(f"  exported          {data.get('meta', {}).get('generated_at')}")
    if data.get("audit"):
        print("  ⚠ audit reported: " + "; ".join(data["audit"]))

    # prove the rebuild agrees with what production said, rather than assuming it does
    from app import facts as F  # noqa: E402
    async with Session() as s:
        f = await F.build(s)
    want, got = data.get("networth") or {}, f.nw
    for k in ("net", "spendable", "invest", "debts"):
        a, b = round(want.get(k, 0), 2), round(got.get(k, 0), 2)
        flag = "ok" if abs(a - b) < 0.02 else "MISMATCH"
        print(f"  {k:10} export {a:>12,.2f}   rebuilt {b:>12,.2f}   {flag}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("export", type=Path)
    ap.add_argument("--db", default="/tmp/chen.db")
    args = ap.parse_args()
    asyncio.run(load(args.export, args.db))


if __name__ == "__main__":
    main()

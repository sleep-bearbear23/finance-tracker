"""Add columns to tables that already exist.

``Base.metadata.create_all`` creates missing *tables* but never touches an existing
one, so a new column on Transaction would silently never appear in the Railway
database. This runs the handful of ALTERs we need, guarded by an actual look at the
live schema so it is safe to run on every boot and on both Postgres and SQLite.
"""
from __future__ import annotations

from sqlalchemy import inspect, text

# table -> column -> SQL type (kept portable: no Postgres-only types)
_COLUMNS: dict[str, dict[str, str]] = {
    "transactions": {
        "inflow_kind": "VARCHAR(24)",
        "reimbursable": "BOOLEAN",
        "nets_txn_id": "VARCHAR(128)",
        "effective_at": "TIMESTAMP WITH TIME ZONE",
        "project": "VARCHAR(64)",
    },
}

_INDEXES: list[tuple[str, str, str]] = [
    ("ix_transactions_inflow_kind", "transactions", "inflow_kind"),
    ("ix_transactions_nets_txn_id", "transactions", "nets_txn_id"),
    ("ix_transactions_project", "transactions", "project"),
]


def _existing(sync_conn, table: str) -> set[str]:
    insp = inspect(sync_conn)
    if table not in insp.get_table_names():
        return set()
    return {c["name"] for c in insp.get_columns(table)}


async def run(engine) -> list[str]:
    """Returns the list of things it actually changed (empty on a warm database)."""
    changed: list[str] = []
    async with engine.begin() as conn:
        for table, cols in _COLUMNS.items():
            have = await conn.run_sync(_existing, table)
            if not have:
                continue  # create_all will build it with every column already present
            for col, sqltype in cols.items():
                if col in have:
                    continue
                await conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {sqltype}"))
                changed.append(f"{table}.{col}")
        for name, table, col in _INDEXES:
            have = await conn.run_sync(_existing, table)
            if col not in have:
                continue
            try:
                await conn.execute(
                    text(f"CREATE INDEX IF NOT EXISTS {name} ON {table} ({col})"))
            except Exception:
                pass  # index is an optimisation, never a reason to fail boot
    return changed

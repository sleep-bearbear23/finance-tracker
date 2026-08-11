"""Database tables — the whole spine lives here."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Float, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from .config import now


class Base(DeclarativeBase):
    pass


class KV(Base):
    """Tiny key/value store: SimpleFIN access url, owner LINE id, init flags, etc."""
    __tablename__ = "kv"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)


class Account(Base):
    __tablename__ = "accounts"
    id: Mapped[str] = mapped_column(String(128), primary_key=True)  # SimpleFIN account id
    name: Mapped[str] = mapped_column(String(255), default="")
    org: Mapped[str] = mapped_column(String(255), default="")
    balance: Mapped[float] = mapped_column(Float, default=0.0)
    available_balance: Mapped[float | None] = mapped_column(Float, nullable=True)
    balance_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Transaction(Base):
    __tablename__ = "transactions"
    id: Mapped[str] = mapped_column(String(128), primary_key=True)  # SimpleFIN txn id
    account_id: Mapped[str] = mapped_column(String(128), index=True)
    amount: Mapped[float] = mapped_column(Float)  # negative = spend, positive = income
    merchant_desc: Mapped[str] = mapped_column(Text, default="")
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    # needs_context → prompted → enriched   |   auto (silent)   |   income   |   ignored
    status: Mapped[str] = mapped_column(String(32), default="needs_context", index=True)
    # where the charge came from: simplefin | shortcut (Apple Pay tap) | manual (told via LINE)
    source: Mapped[str] = mapped_column(String(16), default="simplefin", index=True)

    # Money coming in is not all the same thing: pay | reimburse_work | reimburse_family
    # | personal | refund. Only 'pay' is income; the reimburse/refund kinds cancel an
    # earlier charge, and 'personal' is invisible. See taxonomy.INFLOW_LABEL.
    inflow_kind: Mapped[str | None] = mapped_column(String(24), nullable=True, index=True)
    # For a work purchase: will a production pay this back? None = not asked yet.
    reimbursable: Mapped[bool | None] = mapped_column(nullable=True)
    # For a refund/reimbursement: the id of the charge it reverses, when we found it.
    nets_txn_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    # The date the budget should count this on. Only ever set for a refund: a return
    # lands one or two months after the order, so booking the credit on the day it
    # arrived made March look terrible and June look free. The ledger keeps the true
    # posted_at; the budget uses this.
    effective_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    batch_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    batch_seq: Mapped[int | None] = mapped_column(nullable=True)  # 1..n order shown to the user
    prompted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Message(Base):
    """Rolling conversation log so she remembers what was just said (e.g. a report she sent)."""
    __tablename__ = "messages"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    role: Mapped[str] = mapped_column(String(16))  # 'user' | 'assistant'
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class MerchantMemory(Base):
    """What Momo has told her about a merchant/sender, so she never re-asks.
    is_income: None = a spending merchant, True = real income source, False = payback/transfer."""
    __tablename__ = "merchant_memory"
    key: Mapped[str] = mapped_column(String(128), primary_key=True)  # normalized merchant/sender
    category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_income: Mapped[bool | None] = mapped_column(nullable=True)
    necessary: Mapped[bool] = mapped_column(default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class SavingsPlan(Base):
    __tablename__ = "savings_plan"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    target: Mapped[float] = mapped_column(Float, default=0.0)
    cadence: Mapped[str] = mapped_column(String(32), default="biweekly")  # biweekly | monthly | percent
    allocated: Mapped[float] = mapped_column(Float, default=0.0)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)


class Snapshot(Base):
    """A daily point so the dashboard can draw a net-worth / budget trend over time."""
    __tablename__ = "snapshots"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    day: Mapped[str] = mapped_column(String(10), index=True)  # 'YYYY-MM-DD', one row per day
    net_worth: Mapped[float] = mapped_column(Float, default=0.0)
    assets: Mapped[float] = mapped_column(Float, default=0.0)
    debts: Mapped[float] = mapped_column(Float, default=0.0)
    cash: Mapped[float] = mapped_column(Float, default=0.0)
    allowance: Mapped[float] = mapped_column(Float, default=0.0)
    spent: Mapped[float] = mapped_column(Float, default=0.0)
    income_biweekly: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class BudgetPeriod(Base):
    __tablename__ = "budget_periods"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    income_basis: Mapped[float] = mapped_column(Float, default=0.0)
    allowance: Mapped[float] = mapped_column(Float, default=0.0)
    spent: Mapped[float] = mapped_column(Float, default=0.0)

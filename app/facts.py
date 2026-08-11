"""One read of the world, one set of rules — everything the dashboard shows comes from here.

The dashboard grew block by block, and every block ended up computing its own version of
the same number. That produced exactly the failures Momo hit: the accounts card filtered
to ``kind in ("cash", "credit")`` and silently dropped her brokerage, while the net-worth
card counted it — so the two disagreed by the whole Self-Directed balance. Elsewhere the
trends endpoint summed ``abs(t.amount)`` on ``posted_at`` while the flows chart *in the
same response* used the budget helpers, so refunds netted in one chart and not the other.

The fix isn't another patch. It's this module: load accounts and transactions once,
resolve the registry once, and derive every figure through the same three helpers
(:func:`budget.eff_date`, :func:`budget.spend_amount`, :func:`budget.is_income`). Routes
slice a Facts object; no route is allowed to sum a transaction itself.

If two numbers on the screen disagree after this, it's a bug in one place, not five.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta

from . import accounts as acct
from . import budget
from . import networth
from . import period as P
from . import taxonomy as T
from .config import now

#: account kinds that hold real money (as opposed to 'record', an import bucket)
REAL_KINDS = ("cash", "credit", "invest")


@dataclass
class Facts:
    """A consistent snapshot. Build it once per request, read it many times."""

    registry: dict[str, dict]
    buckets: dict[str, list]
    txns: list
    nw: dict
    today: date

    _resolve: object = field(default=None, repr=False)

    # ── accounts ─────────────────────────────────────────────────────
    def real_accounts(self) -> list[dict]:
        """Every account holding money — cash, credit AND investment.

        The old accounts endpoint left 'invest' out of this list, which is why Momo's
        brokerage vanished from the account card while still counting in net worth."""
        order = {"cash": 0, "invest": 1, "credit": 2}
        return sorted((a for a in self.registry.values() if a["kind"] in REAL_KINDS),
                      key=lambda a: (order[a["kind"]], -(a["balance"] or 0)))

    def record_accounts(self) -> list[dict]:
        return [a for a in self.registry.values()
                if a["kind"] == "record" and a.get("n_txns")]

    def rows_for(self, account_id: str | None) -> list:
        return self.buckets.get(account_id, []) if account_id else self.txns

    # ── the money questions, asked once ──────────────────────────────
    def totals(self) -> dict:
        """Balances, straight off :mod:`networth`, so the accounts card and the
        net-worth card are literally the same arithmetic."""
        return {
            "cash_total": self.nw["spendable"],
            "invest_total": self.nw["invest"],
            "debt_total": self.nw["debts"],
            "assets": self.nw["assets"],
            "net": self.nw["net"],
            "runway_net": self.nw["runway_net"],
            "haircut": self.nw["haircut"],
        }

    def flows(self, keys: list[str], account_id: str | None = None) -> list[dict]:
        """Income / spend per half-month. Refunds subtract and land in the month of the
        charge they reverse, because that's what :func:`budget.eff_date` says."""
        lo, _ = P.key_bounds(keys[0])
        _, hi = P.key_bounds(keys[-1])
        out = {k: {"key": k, "label": P.label(k), "month_start": P.is_month_start(k),
                   "income": 0.0, "spend": 0.0} for k in keys}
        for t in self.rows_for(account_id):
            d = budget.eff_date(t)
            if not d or d < lo or d > hi:
                continue
            k = P.key_for(d)
            if k not in out:
                continue
            if budget.is_spend(t):
                out[k]["spend"] += budget.spend_amount(t)
            elif budget.is_income(t):
                out[k]["income"] += t.amount
        for v in out.values():
            v["income"], v["spend"] = round(v["income"], 2), round(v["spend"], 2)
        return [out[k] for k in keys]

    def monthly(self, n: int = 6, account_id: str | None = None) -> list[dict]:
        inc, out = defaultdict(float), defaultdict(float)
        for t in self.rows_for(account_id):
            d = budget.eff_date(t)
            if not d:
                continue
            ym = d.strftime("%Y-%m")
            if budget.is_spend(t):
                out[ym] += budget.spend_amount(t)
            elif budget.is_income(t):
                inc[ym] += t.amount
        months = sorted(set(inc) | set(out))[-n:]
        return [{"month": m, "income": round(inc.get(m, 0.0), 2),
                 "spend": round(out.get(m, 0.0), 2)} for m in months]

    def category_spend(self, days: int = 90, account_id: str | None = None) -> list[dict]:
        since = self.today - timedelta(days=days)
        agg: dict[str, float] = defaultdict(float)
        for t in self.rows_for(account_id):
            if not budget.is_spend(t):
                continue
            d = budget.eff_date(t)
            if not d or d < since:
                continue
            agg[t.category or "未分類"] += budget.spend_amount(t)
        # A category can only go negative if a return outruns the window's charges —
        # show it rather than clamp, so the number stays explainable.
        return [{"category": c, "label": T.label(c) if c != "未分類" else c,
                 "treatment": T.treatment(c), "amount": round(v, 2)}
                for c, v in sorted(agg.items(), key=lambda x: -x[1])]

    def spend_in(self, lo: date, hi: date, account_id: str | None = None,
                 only_discretionary: bool = False) -> float:
        f = budget.is_discretionary if only_discretionary else budget.is_spend
        return round(sum(budget.spend_amount(t) for t in self.rows_for(account_id)
                         if f(t) and (d := budget.eff_date(t)) and lo <= d <= hi), 2)

    # ── consistency guard ────────────────────────────────────────────
    def audit(self) -> list[str]:
        """Cross-checks that must hold for the screen to be coherent. Returned (not
        raised) so the dashboard can show a warning instead of lying quietly."""
        problems = []
        listed = self.real_accounts()
        cash = sum(a["balance"] or 0 for a in listed if a["kind"] == "cash")
        inv = sum(a["balance"] or 0 for a in listed if a["kind"] == "invest")
        debt = sum(a["balance"] or 0 for a in listed if a["kind"] == "credit")
        if abs(cash - self.nw["spendable"]) > 0.01:
            problems.append(f"帳戶列表現金 {cash:.2f} ≠ 淨值現金 {self.nw['spendable']:.2f}")
        if abs(inv - self.nw["invest"]) > 0.01:
            problems.append(f"帳戶列表投資 {inv:.2f} ≠ 淨值投資 {self.nw['invest']:.2f}")
        if abs(debt - self.nw["debts"]) > 0.01:
            problems.append(f"帳戶列表欠款 {debt:.2f} ≠ 淨值欠款 {self.nw['debts']:.2f}")
        if abs((cash + inv - debt) - self.nw["net"]) > 0.01:
            problems.append("帳戶加總 ≠ 淨資產")
        if len(listed) != len(self.nw["rows"]):
            problems.append(f"帳戶列表 {len(listed)} 個，淨值列了 {len(self.nw['rows'])} 個")
        # every transaction must land in exactly one bucket
        bucketed = sum(len(v) for v in self.buckets.values())
        if bucketed != len(self.txns):
            problems.append(f"{len(self.txns)} 筆交易只分到 {bucketed} 個帳戶")
        return problems


async def build(session) -> Facts:
    """One database read for the whole page."""
    reg, buckets = await acct.build(session)          # accounts + every transaction, bucketed
    txns = [t for rows in buckets.values() for t in rows]
    nw = await networth.compute(session, reg)         # same registry object, not a re-read
    return Facts(registry=reg, buckets=buckets, txns=txns, nw=nw, today=now().date())

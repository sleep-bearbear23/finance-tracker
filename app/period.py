"""Half-month periods — the app's single heartbeat.

Momo's cadence is 每月 1–15 / 16–月底: 24 periods a year that always snap to month
borders and never drift. Every budget window, chart axis and trend in the app uses
these, so the monthly rhythm stays readable (每兩格就是一個月).

A period is identified by a key like '2026-08A' (1st–15th) or '2026-08B' (16th–EOM),
and labelled 8上 / 8下. Monthly amounts are split by real day count, so the shorter
half is never charged as if it were the longer one.
"""
from __future__ import annotations

import calendar
from datetime import date, timedelta

HALVES = ("A", "B")


def bounds(d: date) -> tuple[date, date]:
    """The (start, end_inclusive) of the half-month containing d."""
    if d.day <= 15:
        return date(d.year, d.month, 1), date(d.year, d.month, 15)
    last = calendar.monthrange(d.year, d.month)[1]
    return date(d.year, d.month, 16), date(d.year, d.month, last)


def key_for(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}{'A' if d.day <= 15 else 'B'}"


def parse(key: str) -> tuple[int, int, str]:
    return int(key[:4]), int(key[5:7]), key[7]


def key_bounds(key: str) -> tuple[date, date]:
    y, m, half = parse(key)
    if half == "A":
        return date(y, m, 1), date(y, m, 15)
    return date(y, m, 16), date(y, m, calendar.monthrange(y, m)[1])


def days_in(key: str) -> int:
    s, e = key_bounds(key)
    return (e - s).days + 1


def month_fraction(key: str) -> float:
    """This half's share of its month, by real days (15/31 vs 16/31, etc.)."""
    y, m, _ = parse(key)
    return days_in(key) / calendar.monthrange(y, m)[1]


def label(key: str) -> str:
    y, m, half = parse(key)
    return f"{m}{'上' if half == 'A' else '下'}"


def is_month_start(key: str) -> bool:
    """True for the first half of a month — charts draw the heavier divider here."""
    return key.endswith("A")


def next_key(key: str) -> str:
    y, m, half = parse(key)
    if half == "A":
        return f"{y:04d}-{m:02d}B"
    return f"{y + 1:04d}-01A" if m == 12 else f"{y:04d}-{m + 1:02d}A"


def prev_key(key: str) -> str:
    y, m, half = parse(key)
    if half == "B":
        return f"{y:04d}-{m:02d}A"
    return f"{y - 1:04d}-12B" if m == 1 else f"{y:04d}-{m - 1:02d}B"


def series(start_key: str, end_key: str) -> list[str]:
    """Every period key from start to end, inclusive."""
    out, k = [], start_key
    for _ in range(2000):  # safety bound
        out.append(k)
        if k == end_key:
            break
        k = next_key(k)
    return out


def last_n(end_key: str, n: int) -> list[str]:
    """The n periods ending at end_key (oldest first)."""
    out, k = [], end_key
    for _ in range(max(0, n)):
        out.append(k)
        k = prev_key(k)
    return list(reversed(out))


def split_monthly(monthly: float, key: str) -> float:
    """A monthly amount charged to one half-month, weighted by day count."""
    return float(monthly) * month_fraction(key)


def overlap_days(key: str, start: date, end: date) -> int:
    """How many days of [start, end] (inclusive) fall inside this period."""
    ps, pe = key_bounds(key)
    lo, hi = max(ps, start), min(pe, end)
    return max(0, (hi - lo).days + 1)


def keys_covering(start: date, end: date) -> list[str]:
    """Every period touched by the [start, end] range."""
    return series(key_for(start), key_for(end))


def horizon(from_date: date, n: int) -> list[str]:
    """The n periods starting with the one containing from_date."""
    out, k = [], key_for(from_date)
    for _ in range(max(0, n)):
        out.append(k)
        k = next_key(k)
    return out


def days_left(key: str, today: date) -> int:
    """Days remaining in the period, counting today."""
    _s, e = key_bounds(key)
    return max(0, (e - today).days + 1)


def elapsed_days(key: str, today: date) -> int:
    s, e = key_bounds(key)
    if today < s:
        return 0
    return min((today - s).days + 1, days_in(key))

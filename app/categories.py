"""Category taxonomy + a cheap keyword guess used at ingest time."""
from __future__ import annotations

CATEGORIES = [
    "Food & Groceries",
    "Eating Out",
    "Coffee",
    "Rideshare & Transit",
    "Gas",
    "Rent & Utilities",
    "Subscriptions",
    "Shopping",
    "Art & Work Supplies",
    "Health",
    "Fun & Going Out",
    "Gifts",
    "Income",
    "Transfers/Ignore",
]

# merchant keyword -> category. First match wins. Lowercased contains-match.
_RULES: list[tuple[str, str]] = [
    ("whole foods", "Food & Groceries"),
    ("trader joe", "Food & Groceries"),
    ("ralphs", "Food & Groceries"),
    ("safeway", "Food & Groceries"),
    ("99 ranch", "Food & Groceries"),
    ("h mart", "Food & Groceries"),
    ("costco", "Food & Groceries"),
    ("mcdonald", "Eating Out"),
    ("chipotle", "Eating Out"),
    ("uber eats", "Eating Out"),
    ("uber *eats", "Eating Out"),
    ("ubereats", "Eating Out"),
    ("doordash", "Eating Out"),
    ("grubhub", "Eating Out"),
    ("restaurant", "Eating Out"),
    ("starbucks", "Coffee"),
    ("blue bottle", "Coffee"),
    ("coffee", "Coffee"),
    ("philz", "Coffee"),
    ("uber", "Rideshare & Transit"),
    ("lyft", "Rideshare & Transit"),
    ("metro", "Rideshare & Transit"),
    ("chevron", "Gas"),
    ("shell", "Gas"),
    ("76 ", "Gas"),
    ("arco", "Gas"),
    ("gas", "Gas"),
    ("netflix", "Subscriptions"),
    ("spotify", "Subscriptions"),
    ("adobe", "Subscriptions"),
    ("openai", "Subscriptions"),
    ("anthropic", "Subscriptions"),
    ("icloud", "Subscriptions"),
    ("google storage", "Subscriptions"),
    ("edison", "Rent & Utilities"),
    ("socalgas", "Rent & Utilities"),
    ("at&t", "Rent & Utilities"),
    ("t-mobile", "Rent & Utilities"),
    ("verizon", "Rent & Utilities"),
    ("rent", "Rent & Utilities"),
    ("sephora", "Shopping"),
    ("amazon", "Shopping"),
    ("target", "Shopping"),
    ("uniqlo", "Shopping"),
    ("blick", "Art & Work Supplies"),
    ("michaels", "Art & Work Supplies"),
    ("home depot", "Art & Work Supplies"),
    ("cvs", "Health"),
    ("walgreens", "Health"),
    ("pharmacy", "Health"),
]


def guess(merchant: str) -> str | None:
    m = (merchant or "").lower()
    for kw, cat in _RULES:
        if kw in m:
            return cat
    return None

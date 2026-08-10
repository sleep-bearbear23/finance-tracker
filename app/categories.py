"""Category taxonomy + a cheap keyword guess used at ingest time."""
from __future__ import annotations

import re

CATEGORIES = [
    "Food & Groceries",
    "Eating Out",
    "Coffee",
    "Rideshare & Transit",
    "Gas",
    "Rent & Utilities",
    "Subscriptions",
    "Shopping",
    "Beauty",
    "Pets",
    "Art & Work Supplies",
    "Health",
    "Fun & Going Out",
    "Travel",
    "Gifts",
    "Life Admin",
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


# Descriptions that mean "money just moved between accounts" — a credit-card payment,
# an internal transfer, autopay. These are NOT spending and NOT income; they're noise.
_TRANSFER_KEYWORDS = [
    "payment thank you", "payment - thank you", "autopay", "auto pay", "auto-pay",
    "online payment", "mobile payment", "web payment", "web pymt", "bill pay",
    "card payment", "credit card payment", "crd payment", "pymt", "epay", "e-payment",
    "ach payment", "ach pmt", "chase credit crd", "internal transfer", "online transfer",
    "acct transfer", "acct xfer", "account transfer", "transfer to", "transfer from",
    "to savings", "from savings", "to checking", "from checking",
]


FIXED_HINT = {"Rent & Utilities", "Subscriptions"}  # categories that are recurring/necessary


def is_transfer(merchant: str) -> bool:
    m = (merchant or "").lower()
    return any(kw in m for kw in _TRANSFER_KEYWORDS)


def merchant_key(merchant: str) -> str:
    """Normalize a raw description into a stable key for merchant memory + dedup.
    Drops digits, spaces and punctuation so 'WHOLEFOODS #382' and 'Whole Foods' match,
    but keeps sender names distinct so 'ZELLE FROM JOHN' ≠ 'ZELLE FROM MARY'."""
    s = (merchant or "").lower()
    s = re.sub(r"[^a-z一-鿿]+", "", s)  # keep only letters (incl. Chinese)
    return s[:120] or "unknown"

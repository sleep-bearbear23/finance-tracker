"""Create a charge from a non-SimpleFIN source: an Apple Pay tap or a manual LINE log."""
from __future__ import annotations

import re
from uuid import uuid4

from . import categories
from .config import now
from .models import Transaction


# The iOS Shortcut sends each Apple Pay tap as a LINE message beginning with this marker,
# so the bot can tell an automated tap apart from something Momo actually typed.
TAP_MARKER = "[[TAP]]"


def parse_tap_message(text: str) -> dict | None:
    """Parse '[[TAP]] 47.00 | Whole Foods | Apple Card' → {amount, merchant, card}, else None."""
    if not text:
        return None
    s = text.strip()
    if not s.startswith(TAP_MARKER):
        return None
    parts = [p.strip() for p in s[len(TAP_MARKER):].split("|")]
    return {
        "amount": parts[0] if parts and parts[0] else None,
        "merchant": parts[1] if len(parts) > 1 else "",
        "card": parts[2] if len(parts) > 2 and parts[2] else None,
    }


def get_ci(data: dict, key: str):
    """Case-insensitive dict lookup, so 'Amount' / 'amount' / 'AMOUNT' all work."""
    if not isinstance(data, dict):
        return None
    for k, v in data.items():
        if isinstance(k, str) and k.lower() == key.lower():
            return v
    return None


def coerce_amount(value) -> float | None:
    """Accept 47, 47.0, '47.00', '$47.00', 'USD 47' → 47.0; junk → None."""
    if isinstance(value, (int, float)):
        return float(value)
    s = re.sub(r"[^0-9.\-]", "", str(value or ""))
    try:
        return float(s) if s not in ("", "-", ".", "-.") else None
    except ValueError:
        return None


async def record_charge(session, amount, merchant, source, card=None, note=None):
    """
    source='shortcut' (Apple Pay tap): merchant known, but not *what* — she'll still ask.
    source='manual'   (told via LINE): the user already volunteered it — mark enriched.
    """
    clean = coerce_amount(amount)
    if clean is None:
        raise ValueError(f"unparseable amount: {amount!r}")
    amt = -abs(clean)  # always a spend
    merchant = (merchant or "").strip()
    if source == "manual":
        status = "enriched"
        note = note or merchant
    else:
        status = "needs_context"
    t = Transaction(
        id=f"{source}:{uuid4().hex[:16]}",
        account_id=(card or source),
        amount=amt,
        merchant_desc=merchant,
        category=categories.guess(merchant),
        note=note,
        status=status,
        source=source,
        created_at=now(),
    )
    session.add(t)
    await session.commit()
    return t

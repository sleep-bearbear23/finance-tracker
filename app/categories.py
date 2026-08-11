"""Compatibility shim over :mod:`app.taxonomy`.

The taxonomy moved from a flat list of English display names to ids with treatment
tags. Rather than touch a dozen call sites at once (which is exactly the churn that
broke six test suites last time), this module keeps the old names pointing at the
new implementation. New code should import ``taxonomy`` directly.
"""
from __future__ import annotations

from .taxonomy import (  # noqa: F401  (re-exported on purpose)
    CATEGORIES,
    guess,
    is_transfer,
    merchant_key,
    treatment,
)
from . import taxonomy as _t

#: the id every "this isn't really spending" row gets
TRANSFER = "transfer"

#: categories that are recurring/necessary — used to pre-fill MerchantMemory.necessary
FIXED_HINT = set(_t.of_treatment(_t.FIXED))


def valid(cat: str | None) -> bool:
    return cat in _t.ALL


def label(cat: str | None) -> str:
    return _t.label(cat)

#!/usr/bin/env python3
"""
Which suppliers you have used at this address, and which new-customer-only offers
that rules out.

History sources:
  - Electric: suppliers on archived Duquesne bills (usage_archive.BillArchive)
  - Both: PRIOR_ELECTRIC_SUPPLIERS / PRIOR_GAS_SUPPLIERS for anything older than the
    bills you have, as "Supplier Name@YYYY-MM-DD; Other Supplier" (date = last month
    on that supplier; leave it off if unknown)

An offer is new-customer-only when PA Power/Gas Switch marks "New Customers Offer"
or its description says so. If it names a re-eligibility window ("two years from the
sign-up date…"), use older than that window is allowed; otherwise any prior use of
the supplier rules the offer out.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Iterable, List, Optional

_NEW_CUSTOMER_TEXT = re.compile(
    r"new customers? only|exclusively for new customers|only (?:available )?(?:to|for) new customers"
    r"|available (?:to|for) new customers only",
    re.I,
)
_WINDOW = re.compile(
    r"\b(\d+|one|two|three|four|five|six|twelve|eighteen|twenty-four)\s+(years?|months?)\s+(?:from|after|since)",
    re.I,
)
_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "twelve": 12, "eighteen": 18, "twenty-four": 24,
}
_SUFFIXES = {"llc", "inc", "co", "corp", "corporation", "company", "ltd", "lp"}


@dataclass
class PriorSupplier:
    name: str
    last_used: Optional[date]  # None = date unknown
    source: str  # "bill" | "env" | "current bill"


def normalize_supplier(name: str) -> str:
    """'Example Energy of Pennsylvania LLC' and '… of PA LLC' → same key."""
    s = (name or "").lower()
    s = re.split(r"\bd/?b/?a\b", s)[0]
    s = s.replace("&", " and ")
    s = re.sub(r"\bpennsylvania\b", "pa", s)
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    words = [w for w in s.split() if w not in _SUFFIXES]
    return " ".join(words)


def is_new_customer_offer(rate: Any) -> bool:
    flag = (getattr(rate, "new_customers_offer", "") or "").strip().lower()
    return flag in ("yes", "y", "true") or bool(
        _NEW_CUSTOMER_TEXT.search(getattr(rate, "more_info", "") or "")
    )


def reeligibility_window_days(text: str) -> Optional[int]:
    """'…eligible for this offer two years from the sign-up date…' → 730."""
    m = _WINDOW.search(text or "")
    if not m:
        return None
    n = int(m.group(1)) if m.group(1).isdigit() else _WORDS[m.group(1).lower()]
    return n * 365 if m.group(2).lower().startswith("year") else n * 30


def _parse_env(value: str) -> List[PriorSupplier]:
    out = []
    for part in (value or "").split(";"):
        name, _, when = part.strip().partition("@")
        if not name.strip():
            continue
        try:
            last = date.fromisoformat(when.strip()) if when.strip() else None
        except ValueError:
            last = None
        out.append(PriorSupplier(name.strip(), last, "env"))
    return out


def prior_suppliers(
    utility: str,
    bill_history: Iterable[dict] = (),
    current_supplier: str = "",
) -> List[PriorSupplier]:
    """Every supplier used at this address for `utility` ('electric' | 'gas')."""
    out = _parse_env(os.getenv(f"PRIOR_{utility.upper()}_SUPPLIERS", ""))
    for row in bill_history:
        try:
            last = date.fromisoformat(row["last_bill"])
        except (KeyError, ValueError):
            last = None
        out.append(PriorSupplier(row.get("supplier", ""), last, "bill"))
    if current_supplier:
        out.append(PriorSupplier(current_supplier, date.today(), "current bill"))
    return out


def ineligible_reason(
    rate: Any,
    history: List[PriorSupplier],
    today: Optional[date] = None,
) -> Optional[str]:
    """Why `rate` is closed to you, or None if you can take it."""
    if not is_new_customer_offer(rate):
        return None
    key = normalize_supplier(getattr(rate, "supplier", ""))
    used = [p for p in history if normalize_supplier(p.name) == key]
    if not used:
        return None
    window = reeligibility_window_days(getattr(rate, "more_info", "") or "")
    dated = [p.last_used for p in used if p.last_used]
    last = max(dated) if dated else None
    today = today or date.today()
    if window is not None and last is not None and last < today - timedelta(days=window):
        return None
    if any(p.source == "current bill" for p in used):
        when = "your current supplier"
    else:
        when = f"last used {last.isoformat()}" if last else "date unknown"
    if window is None:
        rule = "any prior use"
    elif window >= 365:
        rule = f"re-eligible {window // 365} yr after enrolling"
    else:
        rule = f"re-eligible {window // 30} mo after enrolling"
    return f"new customers only — you've used {used[0].name} ({when}; {rule})"

"""Market trend and own-rate jump messages."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from .report import RankedOption


def trend_note(history: List[Dict[str, Any]], utility: str, current_top: Optional[RankedOption]) -> str:
    if current_top is None or current_top.unit_price is None:
        return ""
    prev = None
    when = ""
    for entry in reversed(history):
        block = (entry or {}).get(utility) or {}
        if block.get("cheapest_price") is not None:
            prev = block
            when = (entry or {}).get("timestamp", "")
            break
    if not prev:
        return "No prior snapshot — trend will appear after the next check."
    # Snapshots before units were recorded were kWh (electric) / therm (gas).
    prev_unit = prev.get("unit") or ("kWh" if utility == "electric" else "therm")
    if prev_unit != current_top.unit:
        return f"Price unit changed from {prev_unit} to {current_top.unit} — trend restarts next check."
    old = float(prev["cheapest_price"])
    new = float(current_top.unit_price)
    delta = new - old
    if abs(delta) < 1e-5:
        return f"Market cheapest unchanged at ${new:.4f} since last check ({when})."
    direction = "rose" if delta > 0 else "fell"
    return (
        f"Market cheapest {direction} from ${old:.4f} ({prev.get('cheapest_supplier', '?')}) "
        f"to ${new:.4f} ({current_top.supplier}) since {when or 'last check'}."
    )


def own_rate_alert(rate_history: List[dict], threshold_pct: float) -> Optional[Tuple[str, str]]:
    """
    (message, bill_id) when the newest bill's supply rate is threshold_pct above the bill
    before it — the classic variable-rate creep. None otherwise.
    """
    if len(rate_history) < 2:
        return None
    prev, last = rate_history[-2], rate_history[-1]
    if prev["rate"] <= 0:
        return None
    pct = 100.0 * (last["rate"] - prev["rate"]) / prev["rate"]
    if pct < threshold_pct:
        return None
    same = prev["supplier"] == last["supplier"]
    return (
        f"Your supply rate rose {pct:.0f}%: ${prev['rate']:.4f}/kWh (bill {prev['bill_date']}) → "
        f"${last['rate']:.4f}/kWh (bill {last['bill_date']})"
        + (f" with {last['supplier']} — typical of a variable rate resetting." if same else "."),
        last["bill_id"],
    )

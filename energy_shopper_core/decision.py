"""
Inputs in, decision out: the per-fuel middle of a rate check.

`decide_gas` takes offers, usage cycles and a stay baseline, and returns the ranked
options, the recommendation and the notes a report or Home Assistant needs. No network,
no files, no environment: the standalone notifier and the Home Assistant decision layer
both call it, so they always agree.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .eligibility import PriorSupplier, ineligible_reason
from .gas import (
    DEFAULT_THERMS_PER_MCF,
    forecast_annual_from_calendar_profile,
    therms_per_gas_unit,
)
from .ranking import (
    best_by_category,
    lock_in_note,
    pick_recommended,
    rank_plans_by_annual,
    rate_to_option,
)
from .report import RankedOption
from .seasons import GAS_HIGH_USE_MONTHS, MONTH_NAMES

Note = Tuple[str, str]  # (level: info | warning | error, message)

# Gas price units the ranking code can convert between, with HA-style spellings.
_GAS_UNIT_ALIASES = {
    "mcf": "mcf",
    "ccf": "ccf",
    "therm": "therm",
    "therms": "therm",
    "thm": "therm",
    "ft³": "ft3",
    "ft3": "ft3",
    "m³": "m3",
    "m3": "m3",
}
CUBIC_FEET_PER_M3 = 35.3147


@dataclass
class UsageCycle:
    """One billing cycle (or calendar month) of gas use, as the forecast reads it."""

    usage_mcf: float
    month: int
    year: int
    to_date: str = ""  # "MM/DD/YYYY" cycle end, if known; groups by its month


@dataclass
class Baseline:
    """What you pay if you don't switch: the price, how to label it, and caveats."""

    price: float  # USD per offer unit
    plan_type: str = "Default service (Price to Compare)"
    flags: List[str] = field(default_factory=list)


@dataclass
class GasDecision:
    unit: str
    annual_therms: float = 0.0
    annual_units: float = 0.0
    usage_summary: str = ""
    seasonal_coverage_note: str = ""
    ranked: List[RankedOption] = field(default_factory=list)
    stay: Optional[RankedOption] = None
    recommended: Optional[RankedOption] = None
    recommend_switch: bool = False
    savings_pct: Optional[float] = None
    savings_annual_usd: Optional[float] = None
    lock_in_note: str = ""
    highlights: List[Tuple[str, RankedOption]] = field(default_factory=list)
    notes: List[Note] = field(default_factory=list)
    offers_eligible: int = 0
    offers_after_filters: int = 0


def normalize_gas_unit(unit: str) -> Optional[str]:
    """'USD/CCF', 'ccf', 'therms' … → 'ccf' / 'mcf' / 'therm' / 'ft3' / 'm3', or None."""
    u = (unit or "").strip().lower()
    if "/" in u:
        u = u.rsplit("/", 1)[1].strip()
    return _GAS_UNIT_ALIASES.get(u)


def _therms_per(unit: str, therms_per_mcf: float) -> float:
    if unit == "ft3":
        return therms_per_mcf / 1000.0
    if unit == "m3":
        return therms_per_mcf * CUBIC_FEET_PER_M3 / 1000.0
    return therms_per_gas_unit(unit, therms_per_mcf)


def convert_gas_price(
    price: float, from_unit: str, to_unit: str, therms_per_mcf: float = DEFAULT_THERMS_PER_MCF
) -> float:
    """USD per from_unit → USD per to_unit (volume units through heat content when needed)."""
    f, t = normalize_gas_unit(from_unit), normalize_gas_unit(to_unit)
    if f is None or t is None:
        raise ValueError(f"Unknown gas unit: {from_unit!r} or {to_unit!r}")
    if f == t:
        return price
    return price / _therms_per(f, therms_per_mcf) * _therms_per(t, therms_per_mcf)


def apply_filters(rates: Sequence[Any], *, no_cancellation_fee: bool = False, renewable_only: bool = False) -> list:
    filtered = list(rates)
    if no_cancellation_fee:
        filtered = [r for r in filtered if not r.has_cancellation_fee()]
    if renewable_only:
        filtered = [r for r in filtered if r.is_renewable()]
    return filtered


def drop_ineligible(rates: Sequence[Any], history: Sequence[PriorSupplier]) -> Tuple[list, List[Note]]:
    """Remove new-customer-only offers from suppliers you've already used."""
    if not history:
        return list(rates), []
    kept = []
    skipped: Dict[str, List[str]] = {}
    for r in rates:
        why = ineligible_reason(r, history)
        if why:
            skipped.setdefault(r.supplier.strip(), []).append(why)
        else:
            kept.append(r)
    notes = [
        ("info", f"Skipped {len(reasons)} {supplier} offer(s): {reasons[0]}")
        for supplier, reasons in skipped.items()
    ]
    return kept, notes


def decide_gas(
    offers: Sequence[Any],
    *,
    unit: str,
    usage: Sequence[Any],
    baseline: Optional[Baseline],
    threshold_pct: float,
    prior: Sequence[PriorSupplier] = (),
    no_cancellation_fee: bool = False,
    renewable_only: bool = False,
    fallback_monthly_therms: Optional[float] = None,
    therms_per_mcf: float = DEFAULT_THERMS_PER_MCF,
    aggregator: str = "median",
    utility_name: str = "People's Gas",
    today: Optional[date] = None,
) -> GasDecision:
    """
    Rank gas offers (priced per `unit`) against your usage and a stay baseline.

    usage: cycles with usage_mcf / month / year / to_date (UsageCycle, or the Peoples
    client's GasUsageData). fallback_monthly_therms is used only when usage is empty.
    """
    d = GasDecision(unit=unit)
    therms_per_unit = therms_per_gas_unit(unit, therms_per_mcf)

    if usage:
        fc = forecast_annual_from_calendar_profile(
            list(usage), aggregator=aggregator, therms_per_mcf=therms_per_mcf
        )
        d.annual_therms = fc.annual_therms
        counts = fc.calendar_month_sample_counts or {}
        sparse = [m for m, n in counts.items() if n < 2]
        missing = [m for m in range(1, 13) if not counts.get(m)]
        d.usage_summary = (
            f"{fc.method}: ~{fc.annual_mcf:.1f} MCF/yr → {d.annual_therms:,.0f} therms "
            f"(THERMS_PER_MCF={therms_per_mcf})"
        )
        if missing:
            names = ", ".join(MONTH_NAMES[m] for m in missing)
            d.seasonal_coverage_note = (
                f"No samples for calendar months {names}; filled with mean of observed months."
            )
            d.notes.append(("warning", d.seasonal_coverage_note))
        if [m for m in GAS_HIGH_USE_MONTHS if m in sparse or m in missing]:
            d.notes.append(
                (
                    "warning",
                    "Heating-season months have sparse or filled usage samples — lock-in advice is weaker.",
                )
            )
    elif fallback_monthly_therms:
        d.annual_therms = fallback_monthly_therms * 12
        d.usage_summary = f"Naive 12× monthly therms (~{d.annual_therms:,.0f}/yr)"
        d.notes.append(
            ("warning", "Seasonal calendar-month forecast unavailable; used a flat monthly average.")
        )

    d.annual_units = d.annual_therms / therms_per_unit if therms_per_unit else 0.0
    if d.annual_units > 0 and unit == "ccf":
        d.usage_summary += f" = {d.annual_units:,.1f} {unit}/yr"

    eligible, skipped_notes = drop_ineligible(offers, prior)
    d.notes.extend(skipped_notes)
    filtered = apply_filters(
        eligible, no_cancellation_fee=no_cancellation_fee, renewable_only=renewable_only
    )
    d.offers_eligible, d.offers_after_filters = len(eligible), len(filtered)
    if eligible and not filtered:
        d.notes.append(("warning", "Filters removed every gas offer."))

    ranked: List[RankedOption] = []
    if d.annual_units > 0:
        ranked = rank_plans_by_annual(filtered, d.annual_units, unit)
    else:
        for r in filtered:
            p = r.get_numeric_price()
            if p is None or p <= 0:
                continue
            ranked.append(rate_to_option(r, 0.0, unit))
        ranked.sort(key=lambda o: o.unit_price or 9e9)
        if filtered:
            d.notes.append(("warning", f"Ranked by listed $/{unit} because annual usage is unknown."))

    stay = None
    if baseline is not None and baseline.price and d.annual_units > 0:
        stay = RankedOption(
            supplier=utility_name,
            plan_type=baseline.plan_type,
            unit=unit,
            annual_supply_usd=round(d.annual_units * baseline.price, 2),
            unit_price=baseline.price,
            is_current_egs=True,
            term_raw="Current",
            flags=list(baseline.flags),
        )
        ranked.append(stay)
        ranked.sort(key=lambda o: o.annual_supply_usd)

    rec, switch, pct, savings = pick_recommended(ranked, stay, threshold_pct)
    d.ranked, d.stay, d.recommended = ranked, stay, rec
    d.recommend_switch, d.savings_pct, d.savings_annual_usd = switch, pct, savings
    d.lock_in_note = lock_in_note("gas", rec, stay, switch, today=today)

    extras: List[Tuple[str, Optional[RankedOption]]] = []
    if d.annual_units > 0 and len(filtered) < len(eligible):
        removed = rank_plans_by_annual([r for r in eligible if r not in filtered], d.annual_units, unit)
        best_kept = min((o.annual_supply_usd for o in ranked if not o.is_current_egs), default=None)
        if removed and (best_kept is None or removed[0].annual_supply_usd < best_kept):
            extras.append(("Best your filters removed", removed[0]))
    d.highlights = best_by_category(ranked, "gas", extras=extras)
    return d

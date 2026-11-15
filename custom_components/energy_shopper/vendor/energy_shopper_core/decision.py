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
    tou_option_from_analysis,
    tou_shift_option,
)
from .report import RankedOption
from .seasons import GAS_HIGH_USE_MONTHS, MONTH_NAMES
from .usage import UsageWindow

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
    utility_name: str = "People's Gas",  # labels the stay row
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


# ---------------------------------------------------------------------------
# Electric
# ---------------------------------------------------------------------------


@dataclass
class CurrentPlan:
    """The supplier you're on now and its supply-only price (from the bill)."""

    supplier: str
    price: float  # USD/kWh
    plan_type: str = "Current EGS supply (from DLC bill)"
    term_raw: str = "Current contract"
    more_info: str = ""
    flags: List[str] = field(default_factory=lambda: ["supply-only ENERGY CHARGE from latest Duquesne bill PDF"])


@dataclass
class ElectricDecision:
    annual_kwh: float = 0.0
    usage_summary: str = ""
    seasonal_coverage_note: str = ""
    blended_rate: Optional[float] = None  # $/kWh all-in, from the usage window's cost
    ranked: List[RankedOption] = field(default_factory=list)
    stay: Optional[RankedOption] = None
    recommended: Optional[RankedOption] = None
    recommend_switch: bool = False
    savings_pct: Optional[float] = None
    savings_annual_usd: Optional[float] = None
    tou_recommended: Optional[bool] = None  # None: Time-of-Use wasn't scored
    lock_in_note: str = ""
    highlights: List[Tuple[str, RankedOption]] = field(default_factory=list)
    usage_notes: List[Note] = field(default_factory=list)  # about the usage window
    notes: List[Note] = field(default_factory=list)  # about offers and ranking
    offers_eligible: int = 0
    offers_after_filters: int = 0

    @property
    def all_notes(self) -> List[Note]:
        return self.usage_notes + self.notes


def decide_electric(
    offers: Sequence[Any],
    *,
    usage: Optional[UsageWindow],
    current: Optional[CurrentPlan],
    price_to_compare: Optional[float],
    threshold_pct: float,
    blended_rate: float = 0.0,
    prior: Sequence[PriorSupplier] = (),
    no_cancellation_fee: bool = False,
    renewable_only: bool = False,
    tou_rates: Any = None,
    tou_result: Any = None,
    utility_name: str = "Duquesne Light",
    today: Optional[date] = None,
) -> ElectricDecision:
    """
    Rank electric offers ($/kWh) against your usage, your current supplier and the
    utility's default service (Price to Compare), with Time-of-Use when scored.

    current: your supplier from the bill, the stay baseline. price_to_compare: the
    utility's default supply, listed as an option and the baseline when you're on it.
    blended_rate: the all-in $/kWh, used as a last-resort baseline (overstates savings).
    tou_rates / tou_result: from tou.TOUAnalyzer over your hourly usage, or None.
    """
    d = ElectricDecision()
    annual_kwh = 0.0
    if usage:
        annual_kwh = usage.annual_kwh
        d.usage_summary = (
            f"{usage.total_kwh:,.0f} kWh over {usage.span_days:.0f} days "
            f"({usage.baseline_label}); annual projection {annual_kwh:,.0f} kWh via {usage.annual_method}"
        )
        if usage.missing_high_use_months:
            names = ", ".join(MONTH_NAMES[m] for m in usage.missing_high_use_months)
            d.seasonal_coverage_note = (
                f"Summer AC months with no observed usage in this window: {names}. "
                "Annual cost is extrapolated; lock-in advice is weaker until those months accumulate."
            )
            d.usage_notes.append(("warning", d.seasonal_coverage_note))
        if usage.span_days < 60:
            d.usage_notes.append(
                (
                    "warning",
                    f"Usage window is only {usage.span_days:.0f} days — annualized costs have wide uncertainty.",
                )
            )
        blended = usage.baseline_cost / usage.total_kwh if usage.total_kwh > 0 else blended_rate
        if blended:
            d.blended_rate = blended
    d.annual_kwh = annual_kwh

    eligible, skipped_notes = drop_ineligible(offers, prior)
    d.notes.extend(skipped_notes)
    filtered = apply_filters(
        eligible, no_cancellation_fee=no_cancellation_fee, renewable_only=renewable_only
    )
    d.offers_eligible, d.offers_after_filters = len(eligible), len(filtered)
    if eligible and not filtered:
        d.notes.append(("warning", "Filters removed every electric offer."))
    ranked: List[RankedOption] = []
    if annual_kwh > 0:
        ranked = rank_plans_by_annual(filtered, annual_kwh, "kWh")
    else:
        for r in filtered:
            p = r.get_numeric_price()
            if p is None or p <= 0:
                continue
            ranked.append(rate_to_option(r, 0.0, "kWh"))
        ranked.sort(key=lambda o: o.unit_price or 9e9)
        d.notes.append(("warning", "Ranked by listed $/kWh because annual kWh is unknown."))

    if tou_result is not None and annual_kwh > 0:
        ranked.append(tou_option_from_analysis(tou_result, tou_rates, annual_kwh))
        ranked.sort(key=lambda o: o.annual_supply_usd)
        d.tou_recommended = bool(tou_result.tou_recommended)

    stay = None
    if current is not None and current.price > 0 and annual_kwh > 0:
        stay = RankedOption(
            supplier=current.supplier,
            plan_type=current.plan_type,
            unit="kWh",
            annual_supply_usd=round(annual_kwh * current.price, 2),
            unit_price=current.price,
            is_current_egs=True,
            term_raw=current.term_raw,
            more_info=current.more_info,
            flags=list(current.flags),
        )
        ranked.append(stay)

    # The utility's default supply is always a competing option ("go back to default").
    if price_to_compare and annual_kwh > 0:
        already = any(
            o.is_utility_default
            or (utility_name.lower() in o.supplier.lower() and "price to compare" in o.plan_type.lower())
            for o in ranked
        )
        if not already:
            ranked.append(
                RankedOption(
                    supplier=utility_name,
                    plan_type="Default supply (Price to Compare)",
                    unit="kWh",
                    annual_supply_usd=round(annual_kwh * price_to_compare, 2),
                    unit_price=price_to_compare,
                    is_utility_default=True,
                    term_raw="Default",
                    flags=["DLC default if you leave your EGS"],
                )
            )

    if stay is None:
        stay = next((o for o in ranked if o.is_utility_default), None)
    if stay is None and price_to_compare and annual_kwh > 0:
        stay = RankedOption(
            supplier=utility_name,
            plan_type="Default supply (PTC / flat DSS)",
            unit="kWh",
            annual_supply_usd=round(annual_kwh * price_to_compare, 2),
            unit_price=price_to_compare,
            is_utility_default=True,
            term_raw="Default",
        )
        ranked.append(stay)
    elif stay is None and blended_rate > 0 and annual_kwh > 0 and not price_to_compare:
        stay = RankedOption(
            supplier="Current blended (overstates vs supply offers)",
            plan_type="All-in effective rate",
            unit="kWh",
            annual_supply_usd=round(annual_kwh * blended_rate, 2),
            unit_price=blended_rate,
            is_utility_default=True,
            term_raw="Current",
            flags=["not supply-only — comparison to EGS offers overstates savings"],
        )

    ranked.sort(key=lambda o: o.annual_supply_usd if annual_kwh > 0 else (o.unit_price or 9e9))

    rec, switch, pct, savings = pick_recommended(ranked, stay, threshold_pct)
    # Time-of-Use sits on the same leaderboard, so pick_recommended already applied the
    # threshold to it: it's recommended exactly when it's the switch it picked.
    if tou_result is not None:
        d.tou_recommended = bool(rec is not None and rec.is_tou and switch)

    d.ranked, d.stay, d.recommended = ranked, stay, rec
    d.recommend_switch, d.savings_pct, d.savings_annual_usd = switch, pct, savings
    d.lock_in_note = lock_in_note("electric", rec, stay, switch, today=today)

    extras: List[Tuple[str, Optional[RankedOption]]] = []
    if tou_result is not None and annual_kwh > 0:
        for shift in (0.25, 0.5):
            extras.append(
                (f"TOU, shift {shift:.0%} of peak", tou_shift_option(tou_result, tou_rates, annual_kwh, shift))
            )
    if annual_kwh > 0 and len(filtered) < len(eligible):
        removed = rank_plans_by_annual([r for r in eligible if r not in filtered], annual_kwh, "kWh")
        best_kept = min((o.annual_supply_usd for o in ranked if not o.is_current_egs), default=None)
        if removed and (best_kept is None or removed[0].annual_supply_usd < best_kept):
            extras.append(("Best your filters removed", removed[0]))
    d.highlights = best_by_category(ranked, "electric", extras=extras)
    return d

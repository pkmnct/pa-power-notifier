"""
Usage-weighted offer ranking, best-by-category, Time-of-Use options and lock-in advice.

Pure functions over offer objects (see models.Rate) and RankedOption rows: no network,
no files, no environment.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any, List, Optional, Sequence, Tuple

from .eligibility import is_new_customer_offer
from .report import RankedOption
from .seasons import (
    AVG_DAYS_PER_MONTH,
    ELECTRIC_HIGH_USE_MONTHS,
    GAS_HIGH_USE_MONTHS,
    MONTH_NAMES,
)


def parse_term_months(raw: str) -> Optional[int]:
    """Best-effort term length in months. None if unknown / unparseable."""
    s = (raw or "").strip().lower()
    if not s:
        return None
    if any(x in s for x in ("month to month", "month-to-month", "monthly", "variable")):
        return 1
    if "default" in s:
        return None
    m = re.search(r"(\d+)\s*year", s)
    if m:
        return int(m.group(1)) * 12
    m = re.search(r"(\d+)\s*month", s)
    if m:
        return int(m.group(1))
    m = re.search(r"^(\d+)$", s)
    if m:
        n = int(m.group(1))
        return n if 1 <= n <= 60 else None
    return None


def parse_intro_price(raw: str) -> Optional[float]:
    s = (raw or "").strip()
    if not s or s.lower() in ("none", "n/a", "no", "", "$0", "0"):
        return None
    m = re.search(r"(\d+\.?\d*)", s.replace(",", ""))
    if not m:
        return None
    try:
        v = float(m.group(1))
    except ValueError:
        return None
    # Heuristic: values like 9.5 are ¢/kWh; 0.095 is $/kWh
    if v > 1.5:
        v = v / 100.0
    return v if v > 0 else None


def option_flags(rate: Any) -> Tuple[List[str], Optional[int], Optional[float]]:
    flags: List[str] = []
    term_months = parse_term_months(getattr(rate, "term_length", "") or "")
    intro = parse_intro_price(getattr(rate, "introductory_price", "") or "")
    raw_term = (getattr(rate, "term_length", "") or "").strip()
    if raw_term and term_months is None and raw_term.lower() not in ("default",):
        flags.append(f"term length unparsed ({raw_term!r}) — not used in lock-in scoring")
    if intro is not None:
        flags.append(
            "introductory/teaser price listed — ranked on the listed rate, not the intro; "
            "do not treat this as the full-term price"
        )
    if callable(getattr(rate, "has_cancellation_fee", None)) and rate.has_cancellation_fee():
        flags.append(f"cancellation/ETF: {rate.cancellation_fee}")
    return flags, term_months, intro


def period_cost(rate: Any, total_kwh: float, span_days: float) -> Optional[float]:
    p = rate.get_numeric_price()
    if p is None or p <= 0:
        return None
    energy = total_kwh * p
    months = span_days / AVG_DAYS_PER_MONTH
    fees = rate.get_numeric_monthly_fee() * months
    return energy + fees


def annualize_cost(period_total: float, span_days: float) -> float:
    if span_days <= 0:
        return period_total
    return period_total * (365.0 / span_days)


def annual_supply_cost(rate: Any, annual_units: float) -> Optional[float]:
    p = rate.get_numeric_price()
    if p is None or p <= 0:
        return None
    return annual_units * p + rate.get_numeric_monthly_fee() * 12.0


def rate_to_option(rate: Any, annual_usd: float, unit: str) -> RankedOption:
    flags, term_months, intro = option_flags(rate)
    supplier = getattr(rate, "supplier", "") or ""
    plan_type = getattr(rate, "type", "") or ""
    is_default = "duquesne light" in supplier.lower() and "price to compare" in plan_type.lower()
    return RankedOption(
        supplier=supplier,
        plan_type=plan_type,
        unit=unit,
        annual_supply_usd=round(annual_usd, 2),
        unit_price=rate.get_numeric_price(),
        monthly_fee=rate.get_numeric_monthly_fee(),
        term_raw=getattr(rate, "term_length", "") or "",
        term_months=term_months,
        cancellation_fee=getattr(rate, "cancellation_fee", "") or "",
        intro_raw=getattr(rate, "introductory_price", "") or "",
        intro_price=intro,
        more_info=getattr(rate, "more_info", "") or "",
        is_utility_default=is_default,
        flags=flags,
        renewable=rate.renewable_tier() if hasattr(rate, "renewable_tier") else "",
        phone=getattr(rate, "phone", "") or "",
        new_customers_only=is_new_customer_offer(rate),
    )


def tou_shift_option(
    result: Any, tou_rates: Any, annual_kwh: float, shift: float
) -> Optional[RankedOption]:
    """TOU cost if `shift` of peak kWh moved to super off-peak (EV charging, pre-cooling)."""
    total = result.total_kwh or 0.0
    if total <= 0 or annual_kwh <= 0:
        return None
    peak = result.peak_kwh * (1 - shift)
    super_off = result.super_off_peak_kwh + result.peak_kwh * shift
    effective = (
        peak * tou_rates.peak_rate
        + result.off_peak_kwh * tou_rates.off_peak_rate
        + super_off * tou_rates.super_off_peak_rate
    ) / total
    return RankedOption(
        supplier="Duquesne Light",
        plan_type=f"Time-of-Use, {shift:.0%} of peak shifted overnight",
        unit="kWh",
        annual_supply_usd=round(annual_kwh * effective, 2),
        unit_price=effective,
        term_raw="Tariff / pilot",
        is_tou=True,
    )


def best_by_category(
    ranked: List[RankedOption],
    utility: str,
    season_months: int = 12,
    extras: Sequence[Tuple[str, Optional[RankedOption]]] = (),
) -> List[Tuple[str, RankedOption]]:
    """
    Cheapest option per category for the report summary. `ranked` holds offers you're
    eligible for (filters applied); extras are pre-built rows (TOU variants, offers the
    filters removed) appended as given.
    """
    market = [o for o in ranked if not (o.is_current_egs or o.is_tou or o.is_utility_default)]

    def cheapest(pred) -> Optional[RankedOption]:
        hits = [o for o in market if pred(o)]
        return min(hits, key=lambda o: o.annual_supply_usd) if hits else None

    def kind(o: RankedOption) -> str:
        return (o.plan_type or "").strip().lower()

    rows: List[Tuple[str, Optional[RankedOption]]] = [
        ("Best overall", cheapest(lambda o: True)),
        ("Best new-customer offer", cheapest(lambda o: o.new_customers_only)),
        ("Best open to existing customers", cheapest(lambda o: not o.new_customers_only)),
        ("Best fixed", cheapest(lambda o: kind(o).startswith("fixed"))),
        (
            f"Best fixed ≥{season_months} mo",
            cheapest(lambda o: kind(o).startswith("fixed") and (o.term_months or 0) >= season_months),
        ),
        ("Best variable", cheapest(lambda o: kind(o).startswith("variable"))),
    ]
    if utility == "electric":
        rows += [
            ("Best 100% renewable", cheapest(lambda o: o.renewable == "100%")),
            ("Best renewable, share not stated", cheapest(lambda o: o.renewable == "partial")),
            ("Best non-renewable", cheapest(lambda o: o.renewable == "none")),
        ]
    default = next((o for o in ranked if o.is_utility_default), None)
    if default is not None:
        rows.append(("Utility default (Price to Compare)", default))
    tou = next((o for o in ranked if o.is_tou), None)
    if tou is not None:
        rows.append(("Time-of-Use (your usage)", tou))
    rows += list(extras)
    return [(label, opt) for label, opt in rows if opt is not None]


def rank_plans_by_annual(rates: Sequence[Any], annual_units: float, unit: str) -> List[RankedOption]:
    rows: List[RankedOption] = []
    for r in rates:
        c = annual_supply_cost(r, annual_units)
        if c is None:
            continue
        rows.append(rate_to_option(r, c, unit))
    rows.sort(key=lambda o: o.annual_supply_usd)
    return rows


def tou_option_from_analysis(result: Any, tou_rates: Any, annual_kwh: float) -> RankedOption:
    """
    Place TOU on the same annual-kWh basis as fixed offers.

    Uses the observed peak/off/super-off mix (share of kWh) applied to annual_kwh,
    not a separate days-analyzed annualization that can disagree with the calendar
    usage projection.
    """
    total = result.total_kwh or 0.0
    if total <= 0 or annual_kwh <= 0:
        scale = 365.0 / result.days_analyzed if result.days_analyzed else 0.0
        annual_tou = result.tou_total_cost * scale
    else:
        peak_share = result.peak_kwh / total
        off_share = result.off_peak_kwh / total
        super_share = result.super_off_peak_kwh / total
        annual_tou = annual_kwh * (
            peak_share * tou_rates.peak_rate
            + off_share * tou_rates.off_peak_rate
            + super_share * tou_rates.super_off_peak_rate
        )
    effective = (annual_tou / annual_kwh) if annual_kwh else None
    detail = (
        f"Peak {result.peak_pct:.0f}% / Off-peak {result.off_peak_pct:.0f}% / "
        f"Super off-peak {result.super_off_peak_pct:.0f}% of kWh "
        f"({result.days_analyzed}d window {result.date_range[0]}–{result.date_range[1]})"
    )
    return RankedOption(
        supplier="Duquesne Light",
        plan_type="Time-of-Use Supply (Rider No. 8)",
        unit="kWh",
        annual_supply_usd=round(annual_tou, 2),
        unit_price=effective,
        monthly_fee=0.0,
        term_raw="Tariff / pilot",
        term_months=None,
        more_info=getattr(tou_rates, "source", "") or "",
        is_tou=True,
        flags=[],
        tou_detail=detail,
    )


def _season_label(utility: str) -> str:
    if utility == "electric":
        return "summer AC (Jun–Sep)"
    return "heating (Nov–Mar)"


def _in_or_next_high_use(utility: str, today: date) -> str:
    months = ELECTRIC_HIGH_USE_MONTHS if utility == "electric" else GAS_HIGH_USE_MONTHS
    if today.month in months:
        return f"the current {_season_label(utility)} season"
    for i in range(1, 13):
        m = (today.month - 1 + i) % 12 + 1
        if m in months:
            return f"the next {_season_label(utility)} season (starting {MONTH_NAMES[m]})"
    return _season_label(utility)


def lock_in_note(
    utility: str,
    recommended: Optional[RankedOption],
    stay: Optional[RankedOption],
    recommend_switch: bool,
    today: Optional[date] = None,
) -> str:
    today = today or date.today()
    season = _in_or_next_high_use(utility, today)
    if not recommend_switch or recommended is None:
        return (
            f"No lock-in: staying on default/current is cheaper than the alert threshold "
            f"through {season}."
        )
    if recommended.is_tou:
        return (
            f"TOU is a tariff option, not a fixed contract. It will not lock a price through "
            f"{season}; re-check if your peak-hour share changes (AC)."
        )
    if recommended.intro_price is not None:
        return (
            f"Caution: an introductory price is listed. Do not lock in based on the teaser; "
            f"confirm the post-intro rate before signing through {season}."
        )
    term = recommended.term_months
    if term is None:
        return (
            f"Recommended plan term could not be parsed ({recommended.term_raw!r}). "
            f"Confirm it covers {season} before switching."
        )
    if term <= 1:
        return (
            f"Month-to-month/variable: flexible, but will not lock a price through {season}. "
            f"A ~12-month fixed at similar annual cost would."
        )
    if term >= 12:
        extra = ""
        if recommended.cancellation_fee:
            extra = f" Early-termination fee: {recommended.cancellation_fee}."
        return (
            f"A {term}-month term would cover {season}. Consider locking if you expect "
            f"default rates to rise.{extra}"
        )
    extra = f" Early-termination fee: {recommended.cancellation_fee}." if recommended.cancellation_fee else ""
    return (
        f"A {term}-month term may only partially cover {season}. "
        f"Weigh flexibility vs locking a 12-month fixed.{extra}"
    )


def pick_recommended(
    ranked: List[RankedOption],
    stay: Optional[RankedOption],
    threshold_pct: float,
) -> Tuple[Optional[RankedOption], bool, Optional[float], Optional[float]]:
    """Return (recommended, recommend_switch, savings_pct, savings_annual)."""
    if not ranked:
        return None, False, None, None
    ranked = sorted(ranked, key=lambda o: o.annual_supply_usd)
    best = ranked[0]
    if stay is None:
        return best, False, None, None
    if stay.annual_supply_usd <= 0:
        return best, False, None, None
    savings = stay.annual_supply_usd - best.annual_supply_usd
    pct = 100.0 * savings / stay.annual_supply_usd
    if best is stay or (
        (best.is_current_egs and stay.is_current_egs)
        or (best.is_utility_default and stay.is_utility_default and not stay.is_current_egs)
    ):
        return stay, False, 0.0, 0.0
    if pct >= threshold_pct and savings > 0:
        return best, True, pct, savings
    return stay, False, pct if savings > 0 else 0.0, max(0.0, savings)

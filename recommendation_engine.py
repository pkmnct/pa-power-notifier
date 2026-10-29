"""
Usage-weighted ranking, TOU-on-leaderboard, seasonal lock-in, and snapshot trend.

Used by the daily job and by the electric/gas compare CLIs.
Does not import pa_power_notifier (callers pass RateChecker / Rate objects).
"""

from __future__ import annotations

import json
import logging
import os
import re
import statistics
from calendar import monthrange
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple

from check_report import CheckReport, RankedOption, UtilitySection

logger = logging.getLogger(__name__)

AVG_DAYS_PER_MONTH = 30.4375
ELECTRIC_HIGH_USE_MONTHS = frozenset({6, 7, 8, 9})  # summer AC
GAS_HIGH_USE_MONTHS = frozenset({11, 12, 1, 2, 3})  # heating
HISTORY_CAP = 90
MONTH_NAMES = {
    1: "January",
    2: "February",
    3: "March",
    4: "April",
    5: "May",
    6: "June",
    7: "July",
    8: "August",
    9: "September",
    10: "October",
    11: "November",
    12: "December",
}

try:
    from duquesne_light_analyzer import DuquesneLightAnalyzer
except ImportError:
    DuquesneLightAnalyzer = None  # type: ignore

try:
    from tou_analyzer import HourlyUsageRecord, HourlyUsageStore, TOUAnalyzer
except ImportError:
    HourlyUsageStore = None  # type: ignore
    HourlyUsageRecord = None  # type: ignore
    TOUAnalyzer = None  # type: ignore

try:
    from usage_archive import BillArchive, DailyElectricUsageStore, GasUsageStore
except ImportError:
    DailyElectricUsageStore = None  # type: ignore
    GasUsageStore = None  # type: ignore
    BillArchive = None  # type: ignore

try:
    from gas_usage_forecast import (
        forecast_annual_from_calendar_profile,
        therms_per_gas_unit,
        therms_per_mcf,
    )
except ImportError:
    forecast_annual_from_calendar_profile = None  # type: ignore
    therms_per_gas_unit = None  # type: ignore
    therms_per_mcf = None  # type: ignore


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Electric usage window
# ---------------------------------------------------------------------------

@dataclass
class UsageWindow:
    total_kwh: float
    span_days: float
    baseline_cost: float
    baseline_label: str
    annual_kwh: float = 0.0
    annual_method: str = ""
    month_kwh: Dict[int, float] = field(default_factory=dict)
    missing_high_use_months: List[int] = field(default_factory=list)


def _span_days_from_hourly(records: List) -> float:
    if not records:
        return 1.0
    dts = []
    for r in records:
        try:
            dts.append(datetime.fromisoformat(r.dt.replace("Z", "+00:00")))
        except (ValueError, AttributeError):
            continue
    if len(dts) < 2:
        return 1.0
    dts.sort()
    return max(1.0, (dts[-1] - dts[0]).total_seconds() / 86400.0)


def _project_annual_kwh_from_hourly(records: List) -> Tuple[float, str, Dict[int, float], List[int]]:
    """
    If 4+ calendar months have usage, typical-month × days-in-month, gap-fill like gas.
    Otherwise annualize the observed window.
    """
    by_month_days: Dict[int, set] = {m: set() for m in range(1, 13)}
    by_month_kwh: Dict[int, float] = {m: 0.0 for m in range(1, 13)}
    for r in records:
        if getattr(r, "kwh", 0) <= 0:
            continue
        try:
            dt = datetime.fromisoformat(r.dt.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            continue
        by_month_kwh[dt.month] += float(r.kwh)
        by_month_days[dt.month].add(dt.date())

    observed = [m for m in range(1, 13) if by_month_kwh[m] > 0]
    missing_high = sorted(m for m in ELECTRIC_HIGH_USE_MONTHS if m not in observed)

    if len(observed) >= 4:
        typical: Dict[int, float] = {}
        for m in observed:
            days_obs = max(1, len(by_month_days[m]))
            daily = by_month_kwh[m] / days_obs
            typical[m] = daily * monthrange(2025, m)[1]
        filler = statistics.mean(typical.values())
        filled = {m: typical.get(m, filler) for m in range(1, 13)}
        return sum(filled.values()), "calendar_month_typical_from_hourly", filled, missing_high

    total = sum(r.kwh for r in records if getattr(r, "kwh", 0) > 0)
    days = _span_days_from_hourly(records)
    annual = total * (365.0 / days) if days > 0 else total
    return annual, f"annualized_{days:.0f}d_window", by_month_kwh, missing_high


def _archive_daily_from_analysis(analysis: Optional[dict]) -> None:
    """Merge GraphQL daily rows into the local daily archive (never truncate)."""
    if DailyElectricUsageStore is None or not analysis:
        return
    rows = analysis.get("usage_data") or []
    if not rows:
        return
    try:
        path = os.getenv("DAILY_USAGE_FILE")  # None → module default
        store = DailyElectricUsageStore(path)
        added = store.merge(rows)
        if added or len(store) > 0:
            store.save()
    except Exception as e:
        logger.warning("Could not archive daily electric usage: %s", e)


def _archive_gas_usage(usage_list: Sequence[Any], path: Optional[str] = None) -> List[Any]:
    """
    Merge live People's Gas cycles into the local archive and return the
    full archived history (live ∪ archive) for forecasting.
    """
    if GasUsageStore is None:
        return list(usage_list or [])
    try:
        store = GasUsageStore(path)
        if usage_list:
            store.merge(usage_list)
            store.save()
        archived = store.all_records_as_gas_usage_data()
        return archived if archived else list(usage_list or [])
    except Exception as e:
        logger.warning("Could not archive gas usage: %s", e)
        return list(usage_list or [])


def _usage_from_daily_store(checker: Any) -> Optional[UsageWindow]:
    if DailyElectricUsageStore is None:
        return None
    try:
        path = getattr(checker, "daily_usage_file", None) or os.getenv("DAILY_USAGE_FILE")
        store = DailyElectricUsageStore(path)
        recs = [r for r in store.all_records() if r.kwh > 0]
        if not recs:
            return None
        total = sum(r.kwh for r in recs)
        paid = sum(r.cost for r in recs)
        # Approximate span from first→last date
        try:
            d0 = date.fromisoformat(recs[0].date[:10])
            d1 = date.fromisoformat(recs[-1].date[:10])
            days = max(1.0, float((d1 - d0).days + 1))
        except Exception:
            days = float(len(recs))
        # Month buckets for seasonal projection (reuse hourly helper shape)
        class _DayProxy:
            def __init__(self, d: str, kwh: float, cost: float):
                self.dt = d
                self.kwh = kwh
                self.cost = cost

        proxies = [_DayProxy(r.date, r.kwh, r.cost) for r in recs]
        annual, method, month_kwh, missing = _project_annual_kwh_from_hourly(proxies)
        if paid <= 0 and total > 0:
            ce = checker.get_current_electric_rate()
            paid = total * ce if ce and ce > 0 else 0.0
            label = "archived daily kWh × current blended $/kWh"
        else:
            label = "sum of archived daily energy $"
        return UsageWindow(
            total_kwh=total,
            span_days=days,
            baseline_cost=paid,
            baseline_label=label,
            annual_kwh=annual,
            annual_method=f"daily_archive_{method}",
            month_kwh=month_kwh,
            missing_high_use_months=missing,
        )
    except Exception as e:
        logger.warning("Daily usage archive unavailable: %s", e)
        return None


def resolve_usage(checker: Any) -> Optional[UsageWindow]:
    """Build total kWh, window length, and annual projection for ranking."""
    path = getattr(checker, "hourly_usage_file", "")

    if HourlyUsageStore is not None and HourlyUsageRecord is not None and path and os.path.exists(path):
        store = HourlyUsageStore(path)
        recs = [r for r in store.all_records() if r.kwh > 0]
        if recs:
            total = sum(r.kwh for r in recs)
            days = _span_days_from_hourly(recs)
            paid = sum(r.cost for r in recs)
            if paid <= 0 and total > 0:
                ce = checker.get_current_electric_rate()
                paid = total * ce if ce and ce > 0 else 0.0
                label = "hourly kWh × current blended $/kWh (no hourly $ in store)"
            else:
                label = "sum of hourly energy $ from usage store"
            annual, method, month_kwh, missing = _project_annual_kwh_from_hourly(recs)
            return UsageWindow(
                total_kwh=total,
                span_days=days,
                baseline_cost=paid,
                baseline_label=label,
                annual_kwh=annual,
                annual_method=method,
                month_kwh=month_kwh,
                missing_high_use_months=missing,
            )

    # Prefer accumulating daily archive over a single live GraphQL window
    archived = _usage_from_daily_store(checker)
    if archived and archived.span_days >= 30:
        return archived

    cached = getattr(checker, "_last_electric_analysis", None)
    if cached and cached.get("usage_analysis"):
        _archive_daily_from_analysis(cached)
        ua = cached["usage_analysis"]
        if ua and ua.total_kwh > 0:
            days = float(ua.days_analyzed) if ua.days_analyzed else 1.0
            days = max(1.0, days)
            annual = ua.total_kwh * (365.0 / days)
            # Re-read archive after merge (may be longer than live window)
            again = _usage_from_daily_store(checker)
            if again and again.span_days >= days:
                return again
            return UsageWindow(
                total_kwh=ua.total_kwh,
                span_days=days,
                baseline_cost=ua.total_cost,
                baseline_label="Duquesne daily usage API (cached GraphQL window)",
                annual_kwh=annual,
                annual_method=f"annualized_{days:.0f}d_graphql",
                missing_high_use_months=sorted(ELECTRIC_HIGH_USE_MONTHS),
            )

    if DuquesneLightAnalyzer is not None:
        try:
            an = DuquesneLightAnalyzer()
            out = an.run_analysis()
            checker._last_electric_analysis = out
            _archive_daily_from_analysis(out)
            ua = out.get("usage_analysis") if out else None
            if ua and ua.total_kwh > 0:
                days = float(ua.days_analyzed) if ua.days_analyzed else 1.0
                days = max(1.0, days)
                annual = ua.total_kwh * (365.0 / days)
                again = _usage_from_daily_store(checker)
                if again and again.span_days >= days:
                    return again
                return UsageWindow(
                    total_kwh=ua.total_kwh,
                    span_days=days,
                    baseline_cost=ua.total_cost,
                    baseline_label="Duquesne daily usage API (GraphQL window)",
                    annual_kwh=annual,
                    annual_method=f"annualized_{days:.0f}d_graphql",
                    missing_high_use_months=sorted(ELECTRIC_HIGH_USE_MONTHS),
                )
        except Exception as e:
            logger.warning("Duquesne usage fetch failed: %s", e)

    fkwh = os.getenv("USAGE_FALLBACK_KWH", "").strip()
    fdays = os.getenv("USAGE_FALLBACK_DAYS", "").strip()
    if fkwh and fdays:
        try:
            kwh = float(fkwh)
            days = max(1.0, float(fdays))
            skip_baseline = os.getenv("USAGE_FALLBACK_SKIP_BASELINE", "").lower() in (
                "1",
                "true",
                "yes",
            )
            if skip_baseline:
                base = 0.0
            else:
                ce = checker.get_current_electric_rate()
                base = kwh * ce if ce and ce > 0 else 0.0
            annual = kwh * (365.0 / days)
            return UsageWindow(
                total_kwh=kwh,
                span_days=days,
                baseline_cost=base,
                baseline_label="USAGE_FALLBACK_KWH / USAGE_FALLBACK_DAYS env",
                annual_kwh=annual,
                annual_method="fallback_env",
                missing_high_use_months=sorted(ELECTRIC_HIGH_USE_MONTHS),
            )
        except ValueError:
            pass

    return None


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
    )


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


def _snapshot_payload(electric: Optional[UtilitySection], gas: Optional[UtilitySection]) -> Dict[str, Any]:
    def compact(sec: Optional[UtilitySection]) -> Dict[str, Any]:
        if not sec or not sec.ranked:
            return {}
        top = sec.ranked[0]
        return {
            "cheapest_supplier": top.supplier,
            "cheapest_plan": top.plan_type,
            "cheapest_price": top.unit_price,
            "unit": sec.unit,
            "cheapest_annual": top.annual_supply_usd,
            "recommended_supplier": sec.recommended.supplier if sec.recommended else None,
            "recommend_switch": sec.recommend_switch,
        }

    return {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "electric": compact(electric),
        "gas": compact(gas),
    }


def load_snapshot_file(path: str) -> Dict[str, Any]:
    if not path or not os.path.exists(path):
        return {"latest": {}, "history": []}
    try:
        with open(path) as f:
            data = json.load(f)
    except Exception as e:
        logger.warning("Error loading previous rates: %s", e)
        return {"latest": {}, "history": []}
    if "history" in data:
        return data
    return {
        "latest": data,
        "history": [{"timestamp": data.get("timestamp", ""), "electric": {}, "gas": {}}],
    }


def save_snapshot(
    path: str,
    electric_rates: list,
    gas_rates: list,
    electric: Optional[UtilitySection],
    gas: Optional[UtilitySection],
) -> None:
    from dataclasses import asdict

    data = load_snapshot_file(path)
    history = list(data.get("history") or [])
    history.append(_snapshot_payload(electric, gas))
    history = history[-HISTORY_CAP:]
    latest = {
        "electric": [asdict(r) for r in electric_rates],
        "gas": [asdict(r) for r in gas_rates],
        "timestamp": datetime.now().isoformat(timespec="seconds"),
    }
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w") as f:
            json.dump({"latest": latest, "history": history}, f, indent=2)
    except Exception as e:
        logger.error("Error saving rates: %s", e)


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


def _filters_list(checker: Any) -> List[str]:
    out = []
    if getattr(checker, "filter_no_cancellation_fee", False):
        out.append("no cancellation fee")
    if getattr(checker, "filter_renewable_only", False):
        out.append("renewable only")
    return out


def _collect_hourly(checker: Any, report: CheckReport) -> List:
    if HourlyUsageStore is None:
        report.add_warning("info", "tou", "tou_analyzer not installed; hourly store unavailable")
        return []
    store = HourlyUsageStore(checker.hourly_usage_file)
    if DuquesneLightAnalyzer is not None:
        try:
            analyzer = DuquesneLightAnalyzer()
            # Page backward through Opower's 29-day hourly windows so gaps
            # (e.g. missing June / mid-August) get filled while AMI still has them.
            new_records = analyzer.fetch_hourly_history()
            if new_records:
                added = store.merge(new_records)
                # Always save so cost fill-ins persist even when added==0
                store.save()
                logger.info("Hourly store: %d records total (added %d new)", len(store), added)
                if added:
                    report.add_warning(
                        "info",
                        "duquesne",
                        f"Archived {added} new hourly readings into local history "
                        f"({len(store)} hours total).",
                    )
            else:
                report.add_warning(
                    "warning",
                    "duquesne",
                    "Hourly usage fetch returned no records. "
                    "Set DUQUESNE_USERNAME and DUQUESNE_PASSWORD (account IDs auto-discover).",
                )
            # Also pull/refresh daily archive while authenticated session is warm
            try:
                out = analyzer.run_analysis()
                checker._last_electric_analysis = out
                _archive_daily_from_analysis(out)
            except Exception as e:
                logger.debug("Daily archive refresh skipped: %s", e)
        except Exception as e:
            report.add_warning("warning", "duquesne", f"Could not update hourly usage store: {e}")
    recs = store.all_records()
    if not recs:
        report.add_warning(
            "warning",
            "tou",
            "No hourly usage data stored yet — TOU vs fixed cannot be scored until a Duquesne hourly fetch succeeds.",
        )
    return recs


def _run_tou(checker: Any, current_electric: float, hourly: List, report: CheckReport):
    if TOUAnalyzer is None:
        return None, None
    tou_rates = checker._get_tou_rates(current_electric)
    if tou_rates is None:
        report.add_warning(
            "warning",
            "tou",
            "TOU rates unavailable (PDF scraper failed and no TOU_PEAK_RATE / "
            "TOU_OFF_PEAK_RATE / TOU_SUPER_OFF_PEAK_RATE env overrides).",
        )
        return None, None
    if getattr(tou_rates, "source", "") == "env vars":
        report.add_warning(
            "info",
            "tou",
            "Using TOU_* env var overrides because the tariff PDF scrape did not return rates.",
        )
    if not hourly:
        return tou_rates, None
    result = TOUAnalyzer().analyze(hourly, tou_rates)
    return tou_rates, result


def build_check_report(checker: Any) -> CheckReport:
    report = CheckReport(
        threshold_pct=float(getattr(checker, "notification_threshold", 5.0)),
        filters=_filters_list(checker),
    )

    prior = load_snapshot_file(getattr(checker, "storage_file", ""))
    prior_history = list(prior.get("history") or [])

    current_electric = checker.get_current_electric_rate()
    current_gas = checker.get_current_gas_rate()
    egs = None
    if hasattr(checker, "get_current_egs_from_bill"):
        try:
            egs = checker.get_current_egs_from_bill()
        except Exception as e:
            report.add_warning("warning", "duquesne", f"Bill EGS lookup failed: {e}")
    electric_rates = checker.fetch_electric_rates()
    gas_rates = checker.fetch_gas_rates()

    if not electric_rates:
        report.add_warning("error", "papowerswitch", "No electric rates returned from PA Power Switch CSV.")
    if not gas_rates:
        report.add_warning("error", "pagasswitch", "No gas rates returned from PA Gas Switch CSV.")
    if current_electric <= 0 and not (egs and egs.supply_rate_per_kwh > 0):
        report.add_warning(
            "error",
            "electric",
            "Current electric price is 0 — set CURRENT_ELECTRIC_PRICE or DUQUESNE_USERNAME/PASSWORD.",
        )
    if current_gas <= 0:
        report.add_warning(
            "error",
            "gas",
            "Current gas price is 0 — set CURRENT_GAS_PRICE or PEOPLES_GAS_USERNAME/PASSWORD.",
        )

    # PTC = DLC default supply (competing option). Current EGS supply = stay baseline.
    ptc = checker.get_electric_supply_reference_rate()
    current_supply = None
    if egs and egs.supply_rate_per_kwh > 0:
        current_supply = egs.supply_rate_per_kwh
    elif hasattr(checker, "get_current_electric_supply_rate"):
        current_supply = checker.get_current_electric_supply_rate()

    if egs:
        report.add_warning(
            "info",
            "duquesne",
            f"Current EGS from bill {egs.bill_id or '?'}: {egs.supplier} @ "
            f"${egs.supply_rate_per_kwh:.4f}/kWh"
            + (f"; bill PTC ${egs.price_to_compare:.4f}/kWh" if egs.price_to_compare else ""),
        )
        # Persist bill PDF + EGS metadata under data/bills/ (merge-only by bill id)
        if BillArchive is not None and egs.bill_id:
            try:
                bill_dir = getattr(checker, "bill_archive_dir", None)
                pdf_hint = os.getenv("DUQUESNE_BILL_PDF", "").strip()
                latest = os.path.join(
                    os.path.dirname(getattr(checker, "hourly_usage_file", "") or "data/x"),
                    "latest_bill.pdf",
                )
                src = pdf_hint if pdf_hint and os.path.exists(pdf_hint) else (
                    latest if os.path.exists(latest) else ""
                )
                # Analyzer archives during fetch; this is a safety net for offline PDFs
                if src:
                    BillArchive(bill_dir).archive_pdf(src, egs.bill_id, egs=egs)
            except Exception as e:
                logger.debug("Bill archive safety-net skipped: %s", e)
    else:
        report.add_warning(
            "warning",
            "duquesne",
            "Could not read current EGS from bill PDF — stay baseline may use PTC/flat DSS "
            "instead of your actual supplier rate. Need Puppeteer + login for DisplayBillPdf.",
        )

    supply_ref = current_supply or ptc
    if current_electric > 0 and not supply_ref and not current_supply:
        report.add_warning(
            "warning",
            "electric",
            "No supply-only reference (bill EGS or PTC) — supplier comparisons against the "
            "blended all-in rate overstate savings.",
        )

    hourly = _collect_hourly(checker, report)
    usage = resolve_usage(checker)
    if usage is None:
        report.add_warning(
            "warning",
            "electric",
            "No electric usage window — ranking will use listed $/kWh only (monthly fees ignored).",
        )

    elec_section = UtilitySection(
        utility="electric",
        unit="kWh",
        current_blended_rate=current_electric if current_electric > 0 else None,
        # Show the supply rate used as the stay baseline (EGS), falling back to PTC.
        supply_reference_rate=current_supply or ptc,
    )
    analysis = getattr(checker, "_last_electric_analysis", None) or {}
    if egs:
        label = egs.supplier
        if egs.rate_schedule:
            label = f"{egs.supplier} ({egs.rate_schedule})"
        elec_section.current_plan_label = label
    else:
        plan_label = analysis.get("rate_plan_label") if analysis else None
        if plan_label:
            elec_section.current_plan_label = plan_label
        else:
            elec_section.current_plan_label = getattr(checker, "electric_rate_type", "") or "RS (configured)"

    annual_kwh = 0.0
    if usage:
        annual_kwh = usage.annual_kwh
        elec_section.annual_units = annual_kwh
        elec_section.usage_summary = (
            f"{usage.total_kwh:,.0f} kWh over {usage.span_days:.0f} days "
            f"({usage.baseline_label}); annual projection {annual_kwh:,.0f} kWh via {usage.annual_method}"
        )
        if usage.missing_high_use_months:
            names = ", ".join(MONTH_NAMES[m] for m in usage.missing_high_use_months)
            elec_section.seasonal_coverage_note = (
                f"Summer AC months with no observed usage in this window: {names}. "
                "Annual cost is extrapolated; lock-in advice is weaker until those months accumulate."
            )
            report.add_warning("warning", "electric", elec_section.seasonal_coverage_note)
        if usage.span_days < 60:
            report.add_warning(
                "warning",
                "electric",
                f"Usage window is only {usage.span_days:.0f} days — annualized costs have wide uncertainty.",
            )
        blended = usage.baseline_cost / usage.total_kwh if usage.total_kwh > 0 else current_electric
        if blended:
            elec_section.current_blended_rate = blended

    filtered_e = checker.apply_filters(electric_rates)
    if electric_rates and not filtered_e:
        report.add_warning("warning", "electric", "Filters removed every electric offer.")
    ranked_e: List[RankedOption] = []
    if annual_kwh > 0:
        ranked_e = rank_plans_by_annual(filtered_e, annual_kwh, "kWh")
    else:
        for r in filtered_e:
            p = r.get_numeric_price()
            if p is None or p <= 0:
                continue
            ranked_e.append(rate_to_option(r, 0.0, "kWh"))
        ranked_e.sort(key=lambda o: o.unit_price or 9e9)
        report.add_warning("warning", "electric", "Ranked by listed $/kWh because annual kWh is unknown.")

    tou_rates, tou_result = _run_tou(checker, current_electric, hourly, report)
    if tou_result is not None and annual_kwh > 0:
        tou_opt = tou_option_from_analysis(tou_result, tou_rates, annual_kwh)
        ranked_e.append(tou_opt)
        ranked_e.sort(key=lambda o: o.annual_supply_usd)
        elec_section.tou_recommended = bool(tou_result.tou_recommended)

    stay_e = None
    # Prefer current EGS from bill as the stay baseline.
    if egs and egs.supply_rate_per_kwh > 0 and annual_kwh > 0:
        stay_e = RankedOption(
            supplier=egs.supplier,
            plan_type="Current EGS supply (from DLC bill)",
            unit="kWh",
            annual_supply_usd=round(annual_kwh * egs.supply_rate_per_kwh, 2),
            unit_price=egs.supply_rate_per_kwh,
            is_current_egs=True,
            term_raw="Current contract",
            more_info=f"Bill {egs.bill_id}" if egs.bill_id else "",
            flags=["supply-only ENERGY CHARGE from latest Duquesne bill PDF"],
        )
        ranked_e.append(stay_e)

    # Ensure DLC PTC / default appears as a competing option (not the stay baseline).
    if ptc and annual_kwh > 0:
        already = any(
            o.is_utility_default or (
                "duquesne light" in o.supplier.lower()
                and "price to compare" in o.plan_type.lower()
            )
            for o in ranked_e
        )
        if not already:
            ranked_e.append(
                RankedOption(
                    supplier="Duquesne Light",
                    plan_type="Default supply (Price to Compare)",
                    unit="kWh",
                    annual_supply_usd=round(annual_kwh * ptc, 2),
                    unit_price=ptc,
                    is_utility_default=True,
                    term_raw="Default",
                    flags=["DLC default if you leave your EGS"],
                )
            )

    if stay_e is None:
        for opt in ranked_e:
            if opt.is_utility_default:
                stay_e = opt
                break
    if stay_e is None and ptc and annual_kwh > 0:
        stay_e = RankedOption(
            supplier="Duquesne Light",
            plan_type="Default supply (PTC / flat DSS)",
            unit="kWh",
            annual_supply_usd=round(annual_kwh * ptc, 2),
            unit_price=ptc,
            is_utility_default=True,
            term_raw="Default",
        )
        ranked_e.append(stay_e)
    elif stay_e is None and current_electric > 0 and annual_kwh > 0 and not ptc:
        stay_e = RankedOption(
            supplier="Current blended (overstates vs supply offers)",
            plan_type="All-in effective rate",
            unit="kWh",
            annual_supply_usd=round(annual_kwh * current_electric, 2),
            unit_price=current_electric,
            is_utility_default=True,
            term_raw="Current",
            flags=["not supply-only — comparison to EGS offers overstates savings"],
        )

    ranked_e.sort(key=lambda o: o.annual_supply_usd if annual_kwh > 0 else (o.unit_price or 9e9))

    rec_e, switch_e, pct_e, sav_e = pick_recommended(ranked_e, stay_e, report.threshold_pct)
    # TOU only forces a switch recommendation when it clears the same threshold
    # on the shared annual-kWh leaderboard (already handled by pick_recommended).
    if rec_e and rec_e.is_tou and stay_e and stay_e.annual_supply_usd > 0:
        tou_sav = stay_e.annual_supply_usd - rec_e.annual_supply_usd
        tou_pct = 100.0 * tou_sav / stay_e.annual_supply_usd
        elec_section.tou_recommended = tou_sav > 0 and tou_pct >= report.threshold_pct
        if elec_section.tou_recommended:
            switch_e = True
            sav_e = tou_sav
            pct_e = tou_pct
    elif tou_result is not None:
        elec_section.tou_recommended = False

    elec_section.ranked = ranked_e
    elec_section.recommended = rec_e
    elec_section.stay_option = stay_e
    elec_section.recommend_switch = switch_e
    elec_section.savings_pct = pct_e
    elec_section.savings_annual_usd = sav_e
    elec_section.lock_in_note = lock_in_note("electric", rec_e, stay_e, switch_e)
    elec_section.trend_note = trend_note(prior_history, "electric", ranked_e[0] if ranked_e else None)
    report.electric = elec_section

    # Rank gas in the unit PA Gas Switch quotes offers in (Mcf for Peoples), and use the
    # supply-only Price to Compare as the stay baseline — the bill-derived rate includes
    # delivery charges that don't change when you switch suppliers.
    gas_ptc, gas_unit, gas_ptc_source = checker.get_gas_price_to_compare()
    therms_per_unit = therms_per_gas_unit(gas_unit) if therms_per_gas_unit else 1.0
    if gas_ptc:
        report.add_warning(
            "info", "gas", f"Supply Price to Compare ${gas_ptc:.4f}/{gas_unit} (from {gas_ptc_source})"
        )
    else:
        report.add_warning(
            "warning",
            "gas",
            f"No gas Price to Compare (set GAS_ZIPCODE, or GAS_PRICE_TO_COMPARE in $/{gas_unit}) — "
            "stay baseline uses the bill-derived all-in rate, which overstates what switching saves.",
        )
    gas_section = UtilitySection(
        utility="gas",
        unit=gas_unit,
        current_plan_label="People's Gas default service",
        current_blended_rate=current_gas * therms_per_unit if current_gas > 0 else None,
        supply_reference_rate=gas_ptc,
    )
    annual_therms = 0.0
    gas_analysis = getattr(checker, "_last_gas_analysis", None)
    usage_list = (gas_analysis or {}).get("usage_data") or []
    # Merge live cycles into local gas archive; forecast from full history
    gas_path = getattr(checker, "gas_usage_file", None)
    usage_list = _archive_gas_usage(usage_list, path=gas_path)
    if forecast_annual_from_calendar_profile is not None and usage_list:
        fc = forecast_annual_from_calendar_profile(
            usage_list,
            aggregator=os.environ.get("GAS_PROFILE_AGGREGATOR", "median"),
        )
        annual_therms = fc.annual_therms
        sparse = [m for m, n in (fc.calendar_month_sample_counts or {}).items() if n < 2]
        missing = [m for m in range(1, 13) if not (fc.calendar_month_sample_counts or {}).get(m)]
        tpm = therms_per_mcf() if therms_per_mcf else "?"
        gas_section.usage_summary = (
            f"{fc.method}: ~{fc.annual_mcf:.1f} MCF/yr → {annual_therms:,.0f} therms "
            f"(THERMS_PER_MCF={tpm})"
        )
        if missing:
            names = ", ".join(MONTH_NAMES[m] for m in missing)
            gas_section.seasonal_coverage_note = (
                f"No samples for calendar months {names}; filled with mean of observed months."
            )
            report.add_warning("warning", "gas", gas_section.seasonal_coverage_note)
        high_sparse = [m for m in GAS_HIGH_USE_MONTHS if m in sparse or m in missing]
        if high_sparse:
            report.add_warning(
                "warning",
                "gas",
                "Heating-season months have sparse or filled usage samples — lock-in advice is weaker.",
            )
    elif current_gas > 0:
        monthly = checker.estimate_monthly_gas_therms()
        if monthly:
            annual_therms = monthly * 12
            gas_section.usage_summary = f"Naive 12× monthly therms (~{annual_therms:,.0f}/yr)"
            report.add_warning(
                "warning",
                "gas",
                "Seasonal calendar-month forecast unavailable; used a flat monthly average.",
            )

    annual_gas_units = annual_therms / therms_per_unit if therms_per_unit else 0.0
    if annual_gas_units > 0:
        gas_section.annual_units = annual_gas_units
        if gas_unit == "ccf":
            gas_section.usage_summary += f" = {annual_gas_units:,.1f} {gas_unit}/yr"

    filtered_g = checker.apply_filters(gas_rates)
    if gas_rates and not filtered_g:
        report.add_warning("warning", "gas", "Filters removed every gas offer.")
    ranked_g: List[RankedOption] = []
    if annual_gas_units > 0:
        ranked_g = rank_plans_by_annual(filtered_g, annual_gas_units, gas_unit)
    else:
        for r in filtered_g:
            p = r.get_numeric_price()
            if p is None or p <= 0:
                continue
            ranked_g.append(rate_to_option(r, 0.0, gas_unit))
        ranked_g.sort(key=lambda o: o.unit_price or 9e9)
        if filtered_g:
            report.add_warning(
                "warning", "gas", f"Ranked by listed $/{gas_unit} because annual usage is unknown."
            )

    stay_g = None
    stay_price = gas_ptc or (current_gas * therms_per_unit if current_gas > 0 else None)
    if stay_price and annual_gas_units > 0:
        stay_g = RankedOption(
            supplier="People's Gas",
            plan_type=(
                "Default service (Price to Compare)" if gas_ptc else "Current rate (bill-derived)"
            ),
            unit=gas_unit,
            annual_supply_usd=round(annual_gas_units * stay_price, 2),
            unit_price=stay_price,
            is_current_egs=True,
            term_raw="Current",
            flags=[] if gas_ptc else ["bill-derived all-in rate includes delivery charges"],
        )
        ranked_g.append(stay_g)
        ranked_g.sort(key=lambda o: o.annual_supply_usd)

    rec_g, switch_g, pct_g, sav_g = pick_recommended(ranked_g, stay_g, report.threshold_pct)
    gas_section.ranked = ranked_g
    gas_section.recommended = rec_g
    gas_section.stay_option = stay_g
    gas_section.recommend_switch = switch_g
    gas_section.savings_pct = pct_g
    gas_section.savings_annual_usd = sav_g
    gas_section.lock_in_note = lock_in_note("gas", rec_g, stay_g, switch_g)
    gas_section.trend_note = trend_note(prior_history, "gas", ranked_g[0] if ranked_g else None)
    report.gas = gas_section

    save_snapshot(checker.storage_file, electric_rates, gas_rates, elec_section, gas_section)
    return report

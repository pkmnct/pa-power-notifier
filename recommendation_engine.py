"""
The daily check for the standalone notifier: gathers usage, bills and offers, archives
them, and builds the CheckReport.

The ranking, eligibility, Time-of-Use and forecasting logic lives in energy_shopper_core;
this module is the part that reads the environment, the utility accounts and DATA_DIR.
Does not import pa_power_notifier (callers pass RateChecker / Rate objects).
"""

from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple

from energy_shopper_core.alerts import own_rate_alert, trend_note
from energy_shopper_core.eligibility import prior_suppliers
from energy_shopper_core.ranking import rank_plans_by_annual  # noqa: F401  (re-exported for the plan scripts)
from energy_shopper_core.decision import Baseline, CurrentPlan, decide_electric, decide_gas
from energy_shopper_core.report import CheckReport, UtilitySection
from energy_shopper_core.seasons import (
    ELECTRIC_HIGH_USE_MONTHS,
)
from energy_shopper_core.tou import TOUAnalyzer
from energy_shopper_core.usage import (
    UsageWindow,
    project_annual_kwh_from_hourly as _project_annual_kwh_from_hourly,
    span_days_from_hourly as _span_days_from_hourly,
)
from gas_usage_forecast import (
    therms_per_gas_unit,
    therms_per_mcf,
)

logger = logging.getLogger(__name__)

HISTORY_CAP = 90

try:
    from duquesne_light_analyzer import DuquesneLightAnalyzer
except ImportError:
    DuquesneLightAnalyzer = None  # type: ignore

try:
    from usage_archive import (
        BillArchive,
        DailyElectricUsageStore,
        GasUsageStore,
        HourlyUsageStore,
        MarketArchive,
        gas_bill_archive_dir,
    )
except ImportError:
    DailyElectricUsageStore = None  # type: ignore
    HourlyUsageStore = None  # type: ignore
    GasUsageStore = None  # type: ignore
    BillArchive = None  # type: ignore
    MarketArchive = None  # type: ignore

try:
    from peoples_gas_analyzer import PeoplesGasAnalyzer
except ImportError:
    PeoplesGasAnalyzer = None  # type: ignore


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

    if HourlyUsageStore is not None and path and os.path.exists(path):
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


def read_snapshot_file(path: str) -> Tuple[Dict[str, Any], bool]:
    """
    (snapshot, readable). A missing file is an empty snapshot and readable; one that
    exists but can't be parsed is empty and not readable, so it isn't saved over (its
    market trend history would be lost).
    """
    empty: Dict[str, Any] = {"latest": {}, "history": []}
    if not path or not os.path.exists(path):
        return empty, True
    try:
        with open(path) as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("not a JSON object")
    except Exception as e:
        logger.warning("Error loading previous rates: %s", e)
        return empty, False
    if "history" in data:
        return data, True
    return {
        "latest": data,
        "history": [{"timestamp": data.get("timestamp", ""), "electric": {}, "gas": {}}],
    }, True


def write_snapshot_file(path: str, data: Dict[str, Any], readable: bool) -> None:
    """Save the snapshot atomically; refuse (and record it) when the file couldn't be read."""
    from usage_archive import record_save_failure, refuse_save, write_json_atomic

    if not readable:
        refuse_save(path)
        return
    try:
        write_json_atomic(path, data)
    except Exception as e:
        record_save_failure(path, e)


def load_snapshot_file(path: str) -> Dict[str, Any]:
    if not path or not os.path.exists(path):
        return {"latest": {}, "history": []}
    try:
        with open(path) as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("not a JSON object")
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

    data, readable = read_snapshot_file(path)
    history = list(data.get("history") or [])
    history.append(_snapshot_payload(electric, gas))
    history = history[-HISTORY_CAP:]
    latest = {
        "electric": [asdict(r) for r in electric_rates],
        "gas": [asdict(r) for r in gas_rates],
        "timestamp": datetime.now().isoformat(timespec="seconds"),
    }
    write_snapshot_file(path, {"latest": latest, "history": history}, readable)


def _electric_supplier_history(checker: Any, report: CheckReport) -> List[dict]:
    """Archive any Duquesne bills not yet saved, then list the suppliers on them."""
    if BillArchive is None:
        return []
    archive = BillArchive(getattr(checker, "bill_archive_dir", None))
    backfill = os.getenv("BILL_HISTORY_BACKFILL", "1").strip().lower() not in ("0", "false", "no", "off")
    if backfill and DuquesneLightAnalyzer is not None:
        try:
            added = DuquesneLightAnalyzer().backfill_bill_archive(archive)
            if added:
                report.add_warning("info", "duquesne", f"Archived {added} older bill(s) for supplier history.")
        except Exception as e:
            report.add_warning("warning", "duquesne", f"Bill history backfill failed: {e}")
    history = archive.supplier_history()
    if history:
        report.add_warning(
            "info",
            "electric",
            "Supplier history from bills: "
            + "; ".join(f"{h['supplier']} ({h['first_bill']} → {h['last_bill']})" for h in history),
        )
    return history


def _rate_alert_is_new(utility: str, bill_id: str) -> bool:
    """True the first time a jump on bill_id is reported; remembered in DATA_DIR/rate_alerts.json."""
    from env_bootstrap import data_dir

    path = data_dir() / "rate_alerts.json"
    try:
        seen = json.loads(path.read_text()) if path.is_file() else {}
    except Exception:
        seen = {}
    if seen.get(utility) == bill_id:
        return False
    seen[utility] = bill_id
    try:
        # Atomic: a half-written file would read as empty and re-send every known alert.
        # (Saved over even when unreadable: it holds no history, and refusing would re-send
        # the same alert every run.)
        from usage_archive import write_json_atomic

        write_json_atomic(path, seen)
    except Exception as e:
        logger.warning("Could not record rate alert: %s", e)
    return True


def _backfill_gas_bills(report: CheckReport) -> None:
    """Download any People's Gas bill PDFs not yet archived."""
    if PeoplesGasAnalyzer is None or BillArchive is None:
        return
    if not os.getenv("PEOPLES_GAS_USERNAME") or os.getenv("BILL_HISTORY_BACKFILL", "1").strip().lower() in (
        "0", "false", "no", "off",
    ):
        return
    try:
        added = PeoplesGasAnalyzer().backfill_bill_archive(BillArchive(gas_bill_archive_dir()))
        if added:
            report.add_warning("info", "gas", f"Archived {added} People's Gas bill PDF(s).")
    except Exception as e:
        report.add_warning("warning", "gas", f"People's Gas bill backfill failed: {e}")


def _filters_list(checker: Any) -> List[str]:
    out = []
    if getattr(checker, "filter_no_cancellation_fee", False):
        out.append("no cancellation fee")
    if getattr(checker, "filter_renewable_only", False):
        out.append("renewable only")
    return out


def report_failed_saves(report: CheckReport) -> None:
    """
    An archive file that couldn't be read is never saved over, and a write can fail (disk
    full, permissions); either way nothing new reaches it. Say so in the report, as an
    error (so it's emailed), not only in the log: otherwise the archive stops growing
    unnoticed while the utilities drop their history.
    """
    try:
        from usage_archive import save_failure_advice, take_failed_saves
    except ImportError:
        return
    for path, cause in take_failed_saves():
        report.add_warning("error", "archive", save_failure_advice(path, cause))


def _collect_hourly(checker: Any, report: CheckReport) -> List:
    if HourlyUsageStore is None:
        report.add_warning("info", "tou", "usage_archive not available; hourly store unavailable")
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

    market = MarketArchive() if MarketArchive is not None else None
    if market is not None:
        for utility, csv_text in (getattr(checker, "last_offers_csv", None) or {}).items():
            market.save_offers_csv(utility, csv_text)

    if not electric_rates:
        report.add_warning(
            "error",
            "papowerswitch",
            "No electric offers from PA Power Switch — check ELECTRIC_ID and ELECTRIC_RATE_TYPE "
            "(or ELECTRIC_SHOP_RESULTS_URL).",
        )
    if not gas_rates:
        report.add_warning(
            "error", "pagasswitch", "No gas offers from PA Gas Switch — check GAS_ID."
        )
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
        # With a login configured, a missing bill means the Duquesne fetch broke — the
        # savings below are then measured against default service, not what you pay.
        report.add_warning(
            "error" if os.getenv("DUQUESNE_USERNAME") else "warning",
            "duquesne",
            "Could not read your current supplier and rate from the latest Duquesne bill — "
            "savings below are vs Duquesne default service, not your actual rate. "
            "Check the Duquesne login and Puppeteer/Chrome.",
        )

    if market is not None and ptc:
        market.log_price_to_compare("electric", ptc, "kWh", "Duquesne bill / rates page")

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

    # Supplier-history notes go between the usage and ranking notes, as they always have.
    history_report = CheckReport()
    elec_history = prior_suppliers(
        _electric_supplier_history(checker, history_report),
        current_supplier=egs.supplier if egs else "",
        configured=os.getenv("PRIOR_ELECTRIC_SUPPLIERS", ""),
    )
    # TOU warnings go after the ranking notes, where the report has always shown them.
    tou_report = CheckReport()
    tou_rates, tou_result = _run_tou(checker, current_electric, hourly, tou_report)
    current_plan = None
    if egs and egs.supply_rate_per_kwh > 0:
        current_plan = CurrentPlan(
            supplier=egs.supplier,
            price=egs.supply_rate_per_kwh,
            more_info=f"Bill {egs.bill_id}" if egs.bill_id else "",
        )
    elec = decide_electric(
        electric_rates,
        usage=usage,
        current=current_plan,
        price_to_compare=ptc,
        threshold_pct=report.threshold_pct,
        blended_rate=current_electric,
        prior=elec_history,
        no_cancellation_fee=getattr(checker, "filter_no_cancellation_fee", False),
        renewable_only=getattr(checker, "filter_renewable_only", False),
        tou_rates=tou_rates,
        tou_result=tou_result,
    )
    for level, message in elec.usage_notes:
        report.add_warning(level, "electric", message)
    report.warnings.extend(history_report.warnings)
    for level, message in elec.notes:
        report.add_warning(level, "electric", message)
    report.warnings.extend(tou_report.warnings)
    annual_kwh = elec.annual_kwh
    if usage:
        elec_section.annual_units = annual_kwh
        elec_section.usage_summary = elec.usage_summary
        if elec.seasonal_coverage_note:
            elec_section.seasonal_coverage_note = elec.seasonal_coverage_note
        if elec.blended_rate:
            elec_section.current_blended_rate = elec.blended_rate
    if elec.tou_recommended is not None:
        elec_section.tou_recommended = elec.tou_recommended
    ranked_e = elec.ranked
    elec_section.ranked = ranked_e
    elec_section.recommended = elec.recommended
    elec_section.stay_option = elec.stay
    elec_section.recommend_switch = elec.recommend_switch
    elec_section.savings_pct = elec.savings_pct
    elec_section.savings_annual_usd = elec.savings_annual_usd
    elec_section.lock_in_note = elec.lock_in_note
    elec_section.highlights = elec.highlights
    if BillArchive is not None:
        jump = own_rate_alert(
            BillArchive(getattr(checker, "bill_archive_dir", None)).supply_rate_history(),
            float(os.getenv("RATE_JUMP_ALERT_PCT") or "10"),
        )
        if jump:
            elec_section.rate_alert = jump[0]
            elec_section.rate_alert_is_new = _rate_alert_is_new("electric", jump[1])
            report.add_warning("warning", "electric", jump[0])
    elec_section.trend_note = trend_note(prior_history, "electric", ranked_e[0] if ranked_e else None)
    report.electric = elec_section

    # Rank gas in the unit PA Gas Switch quotes offers in (Mcf for Peoples), and use the
    # supply-only Price to Compare as the stay baseline — the bill-derived rate includes
    # delivery charges that don't change when you switch suppliers.
    gas_ptc, gas_unit, gas_ptc_source = checker.get_gas_price_to_compare()
    if not gas_unit:
        report.add_warning(
            "error",
            "gas",
            "Unknown gas price unit — set GAS_ZIPCODE (read from the PA Gas Switch shop page) or "
            "GAS_PRICE_UNIT (mcf, ccf or therm). Ranking as $/therm, which may be wrong.",
        )
        gas_unit = "therm"
    therms_per_unit = therms_per_gas_unit(gas_unit)
    if market is not None and gas_ptc:
        market.log_price_to_compare("gas", gas_ptc, gas_unit, gas_ptc_source)
    _backfill_gas_bills(report)
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
    gas_analysis = getattr(checker, "_last_gas_analysis", None)
    usage_list = (gas_analysis or {}).get("usage_data") or []
    # Merge live cycles into local gas archive; forecast from full history
    gas_path = getattr(checker, "gas_usage_file", None)
    usage_list = _archive_gas_usage(usage_list, path=gas_path)
    fallback_monthly = None
    if not usage_list and current_gas > 0:
        fallback_monthly = checker.estimate_monthly_gas_therms()

    stay_price = gas_ptc or (current_gas * therms_per_unit if current_gas > 0 else None)
    baseline = None
    if stay_price:
        baseline = Baseline(
            price=stay_price,
            plan_type=(
                "Default service (Price to Compare)" if gas_ptc else "Current rate (bill-derived)"
            ),
            flags=[] if gas_ptc else ["bill-derived all-in rate includes delivery charges"],
        )
    # No supplier on People's bills to read; PRIOR_GAS_SUPPLIERS covers past suppliers.
    gas = decide_gas(
        gas_rates,
        unit=gas_unit,
        usage=usage_list,
        baseline=baseline,
        threshold_pct=report.threshold_pct,
        prior=prior_suppliers(configured=os.getenv("PRIOR_GAS_SUPPLIERS", "")),
        no_cancellation_fee=getattr(checker, "filter_no_cancellation_fee", False),
        renewable_only=getattr(checker, "filter_renewable_only", False),
        fallback_monthly_therms=fallback_monthly,
        therms_per_mcf=therms_per_mcf(),
        aggregator=os.environ.get("GAS_PROFILE_AGGREGATOR", "median"),
    )
    for level, message in gas.notes:
        report.add_warning(level, "gas", message)
    gas_section.usage_summary = gas.usage_summary
    if gas.seasonal_coverage_note:
        gas_section.seasonal_coverage_note = gas.seasonal_coverage_note
    if gas.annual_units > 0:
        gas_section.annual_units = gas.annual_units
    ranked_g = gas.ranked
    gas_section.ranked = ranked_g
    gas_section.recommended = gas.recommended
    gas_section.stay_option = gas.stay
    gas_section.recommend_switch = gas.recommend_switch
    gas_section.savings_pct = gas.savings_pct
    gas_section.savings_annual_usd = gas.savings_annual_usd
    gas_section.lock_in_note = gas.lock_in_note
    gas_section.highlights = gas.highlights
    gas_section.trend_note = trend_note(prior_history, "gas", ranked_g[0] if ranked_g else None)
    report.gas = gas_section

    save_snapshot(checker.storage_file, electric_rates, gas_rates, elec_section, gas_section)
    return report

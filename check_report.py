"""Shared check report: findings, warnings, and text/HTML formatters."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from html import escape
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)


def _esc(value: object) -> str:
    return escape("" if value is None else str(value), quote=True)


@dataclass
class WarningItem:
    level: str  # info | warning | error
    source: str
    message: str

    def line(self) -> str:
        return f"[{self.level.upper()}] {self.source}: {self.message}"


@dataclass
class RankedOption:
    supplier: str
    plan_type: str
    unit: str
    annual_supply_usd: float
    unit_price: Optional[float] = None
    monthly_fee: float = 0.0
    term_raw: str = ""
    term_months: Optional[int] = None
    cancellation_fee: str = ""
    intro_raw: str = ""
    intro_price: Optional[float] = None
    more_info: str = ""
    is_tou: bool = False
    is_utility_default: bool = False
    is_current_egs: bool = False
    flags: List[str] = field(default_factory=list)
    tou_detail: str = ""
    renewable: str = ""  # "100%" | "partial" (renewable, share not stated) | "none" | "" (not reported)
    phone: str = ""
    new_customers_only: bool = False


@dataclass
class UtilitySection:
    utility: str  # electric | gas
    unit: str
    current_plan_label: str = ""
    current_blended_rate: Optional[float] = None
    supply_reference_rate: Optional[float] = None
    usage_summary: str = ""
    annual_units: float = 0.0
    ranked: List[RankedOption] = field(default_factory=list)
    recommended: Optional[RankedOption] = None
    stay_option: Optional[RankedOption] = None
    savings_pct: Optional[float] = None
    savings_annual_usd: Optional[float] = None
    recommend_switch: bool = False
    tou_recommended: bool = False
    lock_in_note: str = ""
    trend_note: str = ""
    seasonal_coverage_note: str = ""
    # (category label, option) — best offer per category, for the summary block
    highlights: List[Tuple[str, RankedOption]] = field(default_factory=list)
    rate_alert: str = ""  # your own supply rate jumped on the latest bill
    rate_alert_is_new: bool = False  # first report for this jump → email


@dataclass
class CheckReport:
    generated_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    threshold_pct: float = 5.0
    filters: List[str] = field(default_factory=list)
    warnings: List[WarningItem] = field(default_factory=list)
    electric: Optional[UtilitySection] = None
    gas: Optional[UtilitySection] = None

    def add_warning(self, level: str, source: str, message: str) -> None:
        self.warnings.append(WarningItem(level=level, source=source, message=message))
        if level == "error":
            logger.error("%s: %s", source, message)
        elif level == "warning":
            logger.warning("%s: %s", source, message)
        else:
            logger.info("%s: %s", source, message)

    def has_errors(self) -> bool:
        return any(w.level == "error" for w in self.warnings)

    def should_email(self) -> bool:
        if self.has_errors():
            return True
        if any(sec and sec.rate_alert_is_new for sec in (self.electric, self.gas)):
            return True
        if self.electric and (self.electric.recommend_switch or self.electric.tou_recommended):
            return True
        if self.gas and self.gas.recommend_switch:
            return True
        return False

    def email_subject(self) -> str:
        parts: List[str] = []
        if any(sec and sec.rate_alert_is_new for sec in (self.electric, self.gas)):
            parts.append("rate jump")
        if self.electric and self.electric.recommend_switch:
            parts.append("electric")
        if self.electric and self.electric.tou_recommended:
            parts.append("TOU")
        if self.gas and self.gas.recommend_switch:
            parts.append("gas")
        if parts:
            return f"PA Utility Savings Alert — {', '.join(parts)}"
        if self.has_errors():
            return "PA Utility Check — data errors"
        return "PA Utility Rate Check"

    def format_text(self) -> str:
        lines = [
            "PA Power & Gas Rate Check",
            f"Generated: {self.generated_at}",
            f"Alert threshold: {self.threshold_pct:.1f}% vs staying on default/current supply",
        ]
        if self.filters:
            lines.append(f"Active filters: {', '.join(self.filters)}")
        lines.append("")

        if self.warnings:
            lines.append("--- WARNINGS / UNEXPECTED DATA ---")
            for w in self.warnings:
                lines.append(f"  {w.line()}")
            lines.append("")
        else:
            lines.append("No warnings recorded for this run.")
            lines.append("")

        if self.electric:
            lines.extend(_format_section_text(self.electric))
            lines.append("")
        if self.gas:
            lines.extend(_format_section_text(self.gas))
            lines.append("")

        if self.should_email():
            lines.append("Email trigger: switch recommendation and/or error-level data issues.")
        else:
            lines.append("Email trigger: none (no switch above threshold, TOU not recommended, no errors).")
        return "\n".join(lines).rstrip() + "\n"

    def format_html(self) -> str:
        chunks = [
            "<html><body style='font-family:system-ui,sans-serif;max-width:800px'>",
            "<h1>PA Power &amp; Gas Rate Check</h1>",
            f"<p>Generated: {_esc(self.generated_at)}<br>",
            f"Alert threshold: {self.threshold_pct:.1f}% vs staying on default/current supply",
        ]
        if self.filters:
            chunks.append(f"<br>Active filters: {_esc(', '.join(self.filters))}")
        chunks.append("</p>")

        if self.warnings:
            chunks.append("<h2>Warnings / unexpected data</h2><ul>")
            for w in self.warnings:
                color = "#b00020" if w.level == "error" else "#8a6d3b" if w.level == "warning" else "#333"
                chunks.append(
                    f"<li style='color:{color}'><strong>{_esc(w.level.upper())}</strong> "
                    f"{_esc(w.source)}: {_esc(w.message)}</li>"
                )
            chunks.append("</ul>")

        if self.electric:
            chunks.extend(_format_section_html(self.electric))
        if self.gas:
            chunks.extend(_format_section_html(self.gas))

        chunks.append("</body></html>")
        return "".join(chunks)


def _vs_stay(sec: UtilitySection, opt: RankedOption) -> str:
    stay = sec.stay_option
    if not stay or opt is stay or not sec.annual_units or stay.annual_supply_usd <= 0:
        return ""
    diff = stay.annual_supply_usd - opt.annual_supply_usd
    return f"save ${diff:,.0f}/yr" if diff >= 0 else f"costs ${-diff:,.0f}/yr more"


def _highlight_text(sec: UtilitySection, opt: RankedOption) -> str:
    price = f"${opt.unit_price:.4f}/{sec.unit}" if opt.unit_price is not None else "n/a"
    annual = f"${opt.annual_supply_usd:,.0f}/yr  " if sec.annual_units else ""
    term = f", {opt.term_raw} mo" if opt.term_raw and opt.term_raw.strip().isdigit() else ""
    fee = f", ${opt.monthly_fee:.2f}/mo fee" if opt.monthly_fee else ""
    vs = _vs_stay(sec, opt)
    phone = f"  ☎ {opt.phone}" if opt.phone else ""
    return (
        f"{annual}{price}  {opt.supplier} — {opt.plan_type}{term}{fee}"
        + (f"  ({vs})" if vs else "")
        + phone
    )


def _format_section_text(sec: UtilitySection) -> List[str]:
    label = sec.utility.upper()
    unit = sec.unit
    lines = [f"=== {label} ==="]
    if sec.rate_alert:
        lines.append(f"!! RATE ALERT   : {sec.rate_alert}")
    if sec.current_plan_label:
        lines.append(f"Current plan     : {sec.current_plan_label}")
    if sec.current_blended_rate is not None:
        lines.append(
            f"Blended all-in   : ${sec.current_blended_rate:.4f}/{unit} "
            "(supply + distribution + transmission + riders)"
        )
    if sec.supply_reference_rate is not None:
        source = (
            "EGS ENERGY CHARGE from bill when available"
            if sec.utility == "electric"
            else "utility Price to Compare"
        )
        lines.append(
            f"Current supply   : ${sec.supply_reference_rate:.4f}/{unit} "
            f"({source} — what switching changes)"
        )
    if sec.usage_summary:
        lines.append(f"Usage            : {sec.usage_summary}")
    if sec.seasonal_coverage_note:
        lines.append(f"Seasonal coverage: {sec.seasonal_coverage_note}")
    if sec.annual_units:
        lines.append(f"Projected annual : {sec.annual_units:,.0f} {unit}")

    lines.append("")
    if sec.highlights:
        lines.append("Best by category (annual supply $; vs staying):")
        for label, opt in sec.highlights:
            lines.append(f"  {label:<34} {_highlight_text(sec, opt)}")
        lines.append("")
    if sec.annual_units:
        lines.append("Leaderboard (estimated annual supply $; fees included; lowest first):")
    else:
        lines.append("Leaderboard (listed unit price; annual usage unknown so $/yr is not comparable):")
    if not sec.ranked:
        lines.append("  (no ranked options)")
    else:
        # Always surface stay + recommended even if outside the top N by cost.
        pinned = []
        for opt in (sec.stay_option, sec.recommended):
            if opt is not None and opt not in pinned:
                pinned.append(opt)
        display: List = []
        for opt in pinned + list(sec.ranked):
            if opt not in display:
                display.append(opt)
            if len(display) >= 12 and all(
                p is None or p in display for p in (sec.stay_option, sec.recommended)
            ):
                break
        display = display[:12]

        for i, opt in enumerate(display, 1):
            mark = []
            if sec.recommended and opt is sec.recommended:
                mark.append("RECOMMENDED")
            if opt.is_tou:
                mark.append("TOU")
            if opt.is_current_egs:
                mark.append("CURRENT")
            if opt.is_utility_default:
                mark.append("DLC DEFAULT")
            suffix = f"  [{', '.join(mark)}]" if mark else ""
            price = f"${opt.unit_price:.4f}/{unit}" if opt.unit_price is not None else "n/a"
            term = opt.term_raw or "n/a"
            annual_part = f"${opt.annual_supply_usd:>8,.0f}/yr  " if sec.annual_units else ""
            lines.append(
                f"  {i:>2}. {annual_part}{price}  "
                f"fee ${opt.monthly_fee:.2f}/mo  {opt.supplier} — {opt.plan_type}  "
                f"term={term}{suffix}"
            )
            for flag in opt.flags:
                lines.append(f"      ! {flag}")
            if opt.tou_detail:
                lines.append(f"      {opt.tou_detail}")
            if opt.more_info and (sec.recommended and opt is sec.recommended):
                lines.append(f"      More info: {opt.more_info}")

    lines.append("")
    if sec.recommend_switch and sec.recommended and sec.savings_annual_usd is not None:
        lines.append(
            f"Recommendation   : SWITCH — save ~${sec.savings_annual_usd:,.0f}/yr "
            f"({sec.savings_pct:.1f}% of supply) vs staying on "
            f"{sec.stay_option.supplier if sec.stay_option else 'current'} "
            f"({sec.recommended.supplier} — {sec.recommended.plan_type})"
        )
    elif sec.tou_recommended and sec.recommended and sec.recommended.is_tou:
        lines.append(
            f"Recommendation   : CONSIDER TOU — ~${abs(sec.savings_annual_usd or 0):,.0f}/yr "
            "supply savings vs flat DSS (distribution/riders unchanged)"
        )
    else:
        lines.append("Recommendation   : STAY — no option beats default/current by the alert threshold.")
    if sec.lock_in_note:
        lines.append(f"Lock-in          : {sec.lock_in_note}")
    if sec.trend_note:
        lines.append(f"Market trend     : {sec.trend_note}")
    return lines


def _format_section_html(sec: UtilitySection) -> List[str]:
    chunks = [f"<h2>{_esc(sec.utility.title())}</h2>"]
    if sec.rate_alert:
        chunks.append(
            "<p style='background:#fdecea;color:#b00020;padding:8px;border-radius:4px'>"
            f"<strong>Rate alert:</strong> {_esc(sec.rate_alert)}</p>"
        )
    chunks.append("<p>")
    if sec.current_plan_label:
        chunks.append(f"Current plan: {_esc(sec.current_plan_label)}<br>")
    if sec.current_blended_rate is not None:
        chunks.append(f"Blended all-in: ${sec.current_blended_rate:.4f}/{_esc(sec.unit)}<br>")
    if sec.supply_reference_rate is not None:
        chunks.append(f"Supply reference: ${sec.supply_reference_rate:.4f}/{_esc(sec.unit)}<br>")
    if sec.usage_summary:
        chunks.append(f"Usage: {_esc(sec.usage_summary)}<br>")
    if sec.seasonal_coverage_note:
        chunks.append(f"Seasonal coverage: {_esc(sec.seasonal_coverage_note)}<br>")
    chunks.append("</p>")

    if sec.highlights:
        chunks.append(
            "<h3>Best by category</h3>"
            "<table border='1' cellpadding='4' cellspacing='0' style='border-collapse:collapse'>"
            "<tr><th>Category</th><th>Annual supply</th><th>Price</th><th>Supplier / plan</th>"
            "<th>vs staying</th><th>Phone</th></tr>"
        )
        for label, opt in sec.highlights:
            price = f"${opt.unit_price:.4f}/{_esc(sec.unit)}" if opt.unit_price is not None else "n/a"
            annual = f"${opt.annual_supply_usd:,.0f}" if sec.annual_units else ""
            term = f" ({_esc(opt.term_raw)} mo)" if opt.term_raw and opt.term_raw.strip().isdigit() else ""
            chunks.append(
                f"<tr><td>{_esc(label)}</td><td>{annual}</td><td>{price}</td>"
                f"<td>{_esc(opt.supplier)} — {_esc(opt.plan_type)}{term}</td>"
                f"<td>{_esc(_vs_stay(sec, opt))}</td><td>{_esc(opt.phone)}</td></tr>"
            )
        chunks.append("</table>")
        chunks.append("<h3>Leaderboard</h3>")

    chunks.append(
        "<table border='1' cellpadding='4' cellspacing='0' style='border-collapse:collapse'>"
        "<tr><th>#</th><th>Annual supply</th><th>Price</th><th>Fee/mo</th>"
        "<th>Supplier / plan</th><th>Term</th><th></th></tr>"
    )
    for i, opt in enumerate(sec.ranked[:12], 1):
        marks = []
        if sec.recommended and opt is sec.recommended:
            marks.append("recommended")
        if opt.is_tou:
            marks.append("TOU")
        if opt.is_current_egs:
            marks.append("current")
        if opt.is_utility_default:
            marks.append("dlc-default")
        price = f"${opt.unit_price:.4f}" if opt.unit_price is not None else "n/a"
        chunks.append(
            "<tr>"
            f"<td>{i}</td>"
            f"<td>${opt.annual_supply_usd:,.0f}</td>"
            f"<td>{_esc(price)}</td>"
            f"<td>${opt.monthly_fee:.2f}</td>"
            f"<td>{_esc(opt.supplier)} — {_esc(opt.plan_type)}</td>"
            f"<td>{_esc(opt.term_raw or 'n/a')}</td>"
            f"<td>{_esc(', '.join(marks))}</td>"
            "</tr>"
        )
        if opt.flags:
            chunks.append(
                f"<tr><td colspan='7' style='color:#8a6d3b'>{_esc(' | '.join(opt.flags))}</td></tr>"
            )
    chunks.append("</table>")

    if sec.recommend_switch and sec.recommended:
        chunks.append(
            f"<p><strong>Recommendation: SWITCH</strong> to {_esc(sec.recommended.supplier)} "
            f"— {_esc(sec.recommended.plan_type)}"
        )
        if sec.savings_annual_usd is not None and sec.savings_pct is not None:
            chunks.append(
                f" (~${sec.savings_annual_usd:,.0f}/yr, {sec.savings_pct:.1f}% of supply).</p>"
            )
        else:
            chunks.append(".</p>")
    elif sec.tou_recommended:
        chunks.append("<p><strong>Recommendation: CONSIDER TOU</strong> vs flat DSS supply.</p>")
    else:
        chunks.append("<p><strong>Recommendation: STAY</strong> on default/current.</p>")
    if sec.lock_in_note:
        chunks.append(f"<p>Lock-in: {_esc(sec.lock_in_note)}</p>")
    if sec.trend_note:
        chunks.append(f"<p>Market trend: {_esc(sec.trend_note)}</p>")
    return chunks
